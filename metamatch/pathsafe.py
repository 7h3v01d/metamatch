"""
pathsafe.py
Filesystem-authority guards shared by all three scanners.

MetaMatch's core safety invariant is "scanned path == authorized object":
every destructive operation targets something that came out of the current
scan, and the scan is rooted at a folder the user explicitly selected. A
symlink (or a Windows reparse point / junction) breaks that invariant -
`Library/linked.mp3 -> /Outside/victim.mp3` looks lexically like a library
member, but opening it for tagging follows the link and mutates a file the
user never authorised, outside the selected root.

The scanners use these two checks together, belt-and-suspenders, to admit a
file only when it is a real object living inside the resolved root:

  * is_link_or_reparse() rejects the link object itself, so we never follow
    one; and
  * resolved_within() canonicalises both the candidate and the root with
    realpath() and requires the candidate to stay inside the root once all
    links are resolved - catching escapes via a linked parent directory too.

Rejecting at scan time is the right choke point: Apply/Undo/Quarantine only
ever act on scanned members, so a file that never enters the scan can never
be mutated, renamed, or moved.
"""

from __future__ import annotations

import os

# Windows sets this bit in st_file_attributes for any reparse point
# (symlinks, directory junctions, mount points). islink() catches true
# symlinks; this catches junctions and other reparse objects islink() may not.
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def is_link_or_reparse(path: str) -> bool:
    """True if `path` is a symlink or (on Windows) any reparse point. Also
    True if the path can't be lstat'd at all - we fail closed, treating an
    unstattable entry as unsafe rather than admitting it."""
    try:
        if os.path.islink(path):
            return True
        if os.name == "nt":
            attrs = getattr(os.lstat(path), "st_file_attributes", 0)
            if attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
                return True
    except OSError:
        return True
    return False


def resolved_within(path: str, root: str) -> bool:
    """True if `path`, with every symlink resolved, still lives inside
    `root` (also fully resolved). Mirrors the journal's containment check so
    scan admission and journal containment agree on what "inside the library"
    means."""
    try:
        path_real = os.path.realpath(os.path.abspath(path))
        root_real = os.path.realpath(os.path.abspath(root))
        return os.path.commonpath([path_real, root_real]) == root_real
    except (ValueError, OSError):
        # commonpath raises ValueError across Windows drives / mixed
        # absolute-relative; treat "can't prove containment" as not contained.
        return False


def is_safe_scan_member(path: str, root: str) -> bool:
    """A file may enter the scan only if it is not itself a link/reparse
    point AND its resolved location stays inside the resolved root."""
    if is_link_or_reparse(path):
        return False
    return resolved_within(path, root)


def prune_unsafe_dirs(root: str, dirnames: list[str]) -> list[str]:
    """Filter an os.walk dirnames list to those safe to descend: drop any
    subdirectory that is a link/reparse point (os.walk won't follow it with
    followlinks=False, but pruning it is explicit and also stops it being
    reported) or whose resolved path escapes the root."""
    return [d for d in dirnames if is_safe_scan_member(os.path.join(root, d), root)]
