"""
fingerprint.py
Content-aware file fingerprinting, used to detect whether a file at a
known path is still the same object MetaMatch expects - not just "same
size and same modification time," which can be defeated by any process
that replaces a file's content while preserving both (either
adversarially, or as a side effect of a tool that deliberately preserves
timestamps when copying/restoring a file).

For files up to SAMPLE_THRESHOLD, the whole file is hashed. Beyond that
(a multi-gigabyte movie, say), only the first/middle/last SAMPLE_SIZE
chunks are hashed - three fast, bounded reads rather than one that scales
with file size, while still catching essentially any real content change
(replacement, truncation, corruption, appended data) with overwhelming
likelihood, at a fixed I/O cost regardless of how large the file is.

This is deliberately not a full guarantee of byte-for-byte identity (an
adversary who knows exactly which 1 MiB windows are sampled, and edits
only outside them, could construct a file that passes) - it's a large
practical improvement over size+mtime alone against both accidental
replacement and casual tampering, at a cost cheap enough to run on every
apply/quarantine/undo.
"""

from __future__ import annotations

import hashlib
import os
from typing import Optional

SAMPLE_SIZE = 1024 * 1024  # 1 MiB per sample
SAMPLE_THRESHOLD = SAMPLE_SIZE * 3  # at or below this, just hash the whole file


def content_fingerprint(path: str) -> Optional[str]:
    """Returns a hex digest identifying a file's actual content, or None
    if the file can't be read. Cheap and bounded regardless of file size."""
    try:
        size = os.path.getsize(path)
        h = hashlib.sha256()
        with open(path, "rb") as f:
            if size <= SAMPLE_THRESHOLD:
                h.update(f.read())
            else:
                h.update(f.read(SAMPLE_SIZE))
                f.seek(size // 2)
                h.update(f.read(SAMPLE_SIZE))
                f.seek(max(size - SAMPLE_SIZE, 0))
                h.update(f.read(SAMPLE_SIZE))
                # Mix in the exact size so two different-sized files that
                # happen to share sampled chunks (e.g. both start and end
                # with silence, or a black frame) don't collide.
                h.update(str(size).encode())
        return h.hexdigest()
    except OSError:
        return None
