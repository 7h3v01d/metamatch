"""
art.py
Fetches album cover art from the Cover Art Archive (coverartarchive.org),
which is keyed off the MusicBrainz release id already captured in a
match. No API key required. Results are cached in memory for the life
of the process since the same release is often looked up twice: once
for the UI thumbnail, once again when the user applies the match.

The cache is bounded (LRU eviction) so a very large library doesn't
accumulate unbounded image data in memory, and a failed lookup is only
cached briefly rather than forever - a transient Cover Art Archive
hiccup shouldn't permanently block retrying for the rest of the process's
life, but a large library also shouldn't hammer the server on every
single re-render of files that genuinely have no art.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Optional

import requests

COVER_ART_BASE = "https://coverartarchive.org/release"
USER_AGENT = "MetaMatch/1.0 ( https://example.local/metamatch )"

_MAX_CACHE_ENTRIES = 300
_NEGATIVE_CACHE_TTL_SECONDS = 300  # how long a failed/missing lookup is remembered before retrying

_cache: "OrderedDict[str, tuple[bytes, str]]" = OrderedDict()
_negative_cache: dict[str, float] = {}  # cache_key -> time.monotonic() of the failed attempt
_cache_lock = threading.Lock()

_MISS = object()  # sentinel: genuinely not cached either way, caller should fetch


def _cache_get(cache_key: str):
    with _cache_lock:
        if cache_key in _cache:
            _cache.move_to_end(cache_key)
            return _cache[cache_key]

        failed_at = _negative_cache.get(cache_key)
        if failed_at is not None:
            if time.monotonic() - failed_at < _NEGATIVE_CACHE_TTL_SECONDS:
                return None  # still within the "recently failed, don't retry yet" window
            del _negative_cache[cache_key]  # TTL expired - fall through and retry
    return _MISS


def _cache_put_success(cache_key: str, value: tuple[bytes, str]) -> None:
    with _cache_lock:
        _negative_cache.pop(cache_key, None)
        _cache[cache_key] = value
        _cache.move_to_end(cache_key)
        while len(_cache) > _MAX_CACHE_ENTRIES:
            _cache.popitem(last=False)


def _cache_put_failure(cache_key: str) -> None:
    with _cache_lock:
        _negative_cache[cache_key] = time.monotonic()


def fetch_cover_art(release_id: str, size: str = "250") -> Optional[tuple[bytes, str]]:
    """
    Returns (image_bytes, mime_type) for the front cover of a release, or
    None if no art is available. size can be '250', '500', 1200' or 'full'
    (Cover Art Archive convention: front, front-250, front-500, front-1200).
    """
    if not release_id:
        return None

    cache_key = f"{release_id}:{size}"
    cached = _cache_get(cache_key)
    if cached is not _MISS:
        return cached

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

    if result is not None:
        _cache_put_success(cache_key, result)
    else:
        _cache_put_failure(cache_key)
    return result
