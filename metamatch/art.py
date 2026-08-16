"""
art.py
Fetches album cover art from the Cover Art Archive (coverartarchive.org),
which is keyed off the MusicBrainz release id already captured in a
match. No API key required. Results are cached in memory for the life
of the process since the same release is often looked up twice: once
for the UI thumbnail, once again when the user applies the match.
"""

from __future__ import annotations

import threading
from typing import Optional

import requests

COVER_ART_BASE = "https://coverartarchive.org/release"
USER_AGENT = "MetaMatch/1.0 ( https://example.local/metamatch )"

_cache: dict[str, Optional[tuple[bytes, str]]] = {}
_cache_lock = threading.Lock()


def fetch_cover_art(release_id: str, size: str = "250") -> Optional[tuple[bytes, str]]:
    """
    Returns (image_bytes, mime_type) for the front cover of a release, or
    None if no art is available. size can be '250', '500', 1200' or 'full'
    (Cover Art Archive convention: front, front-250, front-500, front-1200).
    """
    if not release_id:
        return None

    cache_key = f"{release_id}:{size}"
    with _cache_lock:
        if cache_key in _cache:
            return _cache[cache_key]

    suffix = "front" if size == "full" else f"front-{size}"
    url = f"{COVER_ART_BASE}/{release_id}/{suffix}"
    headers = {"User-Agent": USER_AGENT}

    result = None
    try:
        resp = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
        if resp.status_code == 200 and resp.content:
            mime = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
            result = (resp.content, mime)
    except requests.RequestException:
        result = None

    with _cache_lock:
        _cache[cache_key] = result
    return result
