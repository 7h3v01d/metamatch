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
import stat

# Windows sets this bit in st_file_attributes for any reparse point
# (symlinks, directory junctions, mount points). islink() catches true
# symlinks; this catches junctions and other reparse objects islink() may not.
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _contains(root_real: str, target_real: str) -> bool:
    try:
        return os.path.commonpath([target_real, root_real]) == root_real
    except (ValueError, OSError):
        return False


def is_link_or_reparse(path: str) -> bool:
    """True if `path` is a symlink or (on Windows) any reparse point. A path
    that does not exist is NOT a link - it's absent, and absent means safe to
    create - so this returns False for a missing path. An existing path that
    can't be lstat'd (a permission error, say) fails closed to True.

    The lexists() guard on the Windows branch matters: os.lstat() on a missing
    path raises FileNotFoundError, and treating that as "unsafe" would make
    MetaMatch refuse to CREATE any new sidecar on Windows (the file we're about
    to write doesn't exist yet by definition)."""
    try:
        if os.path.islink(path):
            return True
        if os.name == "nt" and os.path.lexists(path):
            attrs = getattr(os.lstat(path), "st_file_attributes", 0)
            if attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
                return True
    except OSError:
        # An existing path we genuinely can't inspect - fail closed.
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


def validate_mutation_target(path: str, root: str, *, allow_missing: bool = False,
                             reject_hardlinks: bool = True) -> "tuple[bool, str | None]":
    """The single authority gate to call IMMEDIATELY BEFORE mutating a path -
    not just at scan time. Filesystem identity is mutable: a file that was a
    safe library member at scan time can be swapped for a symlink, hard-linked
    to an outside inode, or have its parent directory replaced with a junction
    before the mutation happens. Scan admission proves nothing about the object
    that exists at mutation time, so every destructive operation re-checks here,
    under its mutation lock, before touching anything.

    Returns (ok, reason). A target is safe to mutate when:
      * it is not itself a symlink / reparse point (we never follow a link to
        write through it);
      * its resolved location still lives inside the resolved library root
        (catches a parent directory swapped for a link pointing outside); and
      * (for a regular file, when reject_hardlinks) it has a single hard-link
        name - a file with st_nlink > 1 has aliases we can't prove are all
        inside the library, so mutating it could change an outside object.

    With allow_missing (for a sidecar we're about to CREATE), a nonexistent
    path is validated by its parent directory instead, so we never create a
    file through a linked/escaping parent."""
    try:
        root_real = os.path.realpath(os.path.abspath(root))
    except OSError:
        return False, "the library root is unreadable"

    # lexists so a (possibly broken) symlink is seen as present and rejected,
    # rather than treated as a safe "missing" path.
    if os.path.lexists(path):
        if is_link_or_reparse(path):
            return False, "it is a symlink or reparse point (MetaMatch won't follow a link to mutate it)"
        try:
            target_real = os.path.realpath(os.path.abspath(path))
        except OSError:
            return False, "it is unreadable"
        if not _contains(root_real, target_real):
            return False, "it resolves to a location outside the selected library folder"
        if reject_hardlinks:
            try:
                st = os.stat(path)
            except OSError:
                return False, "it is unreadable"
            if stat.S_ISREG(st.st_mode) and getattr(st, "st_nlink", 1) > 1:
                return False, ("it has multiple hard-link names, so MetaMatch can't prove every "
                               "alias is inside the library")
        return True, None

    if not allow_missing:
        return False, "it no longer exists (rescan the library)"

    # Creating something new: validate the parent directory (must exist, be a
    # real directory inside the root, and not itself a link/reparse).
    parent = os.path.dirname(os.path.abspath(path)) or "."
    ok, reason = validate_mutation_target(parent, root, allow_missing=False, reject_hardlinks=False)
    if not ok:
        return False, f"its parent directory {reason}"
    return True, None


def sidecar_write_is_unsafe(path: str) -> bool:
    """True if writing to this sidecar path would follow a symlink/reparse
    point - i.e. the path already exists AS a link. Cheap guard to place right
    before any sidecar write (.nfo, poster, thumbnail): never overwrite through
    a link, which would clobber whatever it points at, outside the library.
    A nonexistent path (a sidecar we're creating fresh) is safe here; its
    containment is inherited from the already-validated media file that shares
    its directory."""
    return is_link_or_reparse(path)
