"""
matcher.py
Queries the MusicBrainz web service for candidate recordings and scores
each candidate against the local file's tags/filename to find the best
match, with an overall confidence score (0-100).

MusicBrainz requires a descriptive User-Agent and asks for roughly
1 request/second from unauthenticated clients, so all lookups are
rate-limited here.
"""

from __future__ import annotations

import time
import threading
from typing import Optional

import requests
from rapidfuzz import fuzz

MB_SEARCH_URL = "https://musicbrainz.org/ws/2/recording/"
USER_AGENT = "MetaMatch/1.0 ( https://example.local/metamatch )"

_last_request_lock = threading.Lock()
_last_request_time = 0.0
_MIN_INTERVAL = 1.05  # seconds, stay comfortably under MB's rate limit


def _throttle():
    global _last_request_time
    with _last_request_lock:
        elapsed = time.time() - _last_request_time
        if elapsed < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - elapsed)
        _last_request_time = time.time()


def _mb_search(artist: Optional[str], title: Optional[str], limit: int = 5) -> list[dict]:
    """Runs a MusicBrainz recording search and returns raw candidate dicts."""
    if not title:
        return []

    query_parts = [f'recording:"{title}"']
    if artist:
        query_parts.append(f'artist:"{artist}"')
    query = " AND ".join(query_parts)

    params = {"query": query, "fmt": "json", "limit": limit}
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}

    _throttle()
    try:
        resp = requests.get(MB_SEARCH_URL, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data.get("recordings", [])
    except requests.RequestException:
        return []


def _best_release(recording: dict) -> Optional[dict]:
    releases = recording.get("releases") or []
    if not releases:
        return None
    # Prefer releases that look like an original album over compilations,
    # but any release beats none.
    releases_sorted = sorted(
        releases,
        key=lambda r: (r.get("release-group", {}).get("primary-type") != "Album", r.get("date") or "9999"),
    )
    return releases_sorted[0]


def _duration_score(local_seconds: Optional[float], mb_length_ms: Optional[int]) -> Optional[float]:
    if local_seconds is None or not mb_length_ms:
        return None
    mb_seconds = mb_length_ms / 1000.0
    diff = abs(local_seconds - mb_seconds)
    if diff <= 1:
        return 100.0
    if diff >= 15:
        return 0.0
    # Linear falloff between 1s (perfect-ish) and 15s (no confidence)
    return max(0.0, 100.0 * (1 - (diff - 1) / 14))


def score_candidate(track, recording: dict) -> dict:
    query_artist = track.tag_artist or track.guess_artist or ""
    query_title = track.tag_title or track.guess_title or track.filename

    cand_title = recording.get("title") or ""
    cand_artist_credit = recording.get("artist-credit") or []
    cand_artist = " ".join(a.get("name", "") for a in cand_artist_credit if isinstance(a, dict)) or \
        "".join(str(a) for a in cand_artist_credit if isinstance(a, str))

    release = _best_release(recording)
    cand_album = release.get("title") if release else None
    cand_date = release.get("date") if release else None
    cand_release_id = release.get("id") if release else None

    title_sim = fuzz.token_sort_ratio(query_title, cand_title) if query_title and cand_title else 0
    artist_sim = fuzz.token_sort_ratio(query_artist, cand_artist) if query_artist and cand_artist else None

    dur_sim = _duration_score(track.duration_seconds, recording.get("length"))
    mb_score = float(recording.get("score", 0))

    # Weighted blend: title match matters most, artist next, then MB's own
    # relevance score and duration as corroborating signals.
    weights_used = []
    weighted_sum = 0.0

    weighted_sum += title_sim * 0.40
    weights_used.append(0.40)

    if artist_sim is not None:
        weighted_sum += artist_sim * 0.30
        weights_used.append(0.30)

    weighted_sum += mb_score * 0.20
    weights_used.append(0.20)

    if dur_sim is not None:
        weighted_sum += dur_sim * 0.10
        weights_used.append(0.10)

    total_weight = sum(weights_used)
    confidence = round(weighted_sum / total_weight, 1) if total_weight else 0.0

    return {
        "recording_id": recording.get("id"),
        "release_id": cand_release_id,
        "title": cand_title,
        "artist": cand_artist,
        "album": cand_album,
        "date": cand_date,
        "length_ms": recording.get("length"),
        "mb_score": mb_score,
        "title_similarity": round(title_sim, 1),
        "artist_similarity": round(artist_sim, 1) if artist_sim is not None else None,
        "duration_similarity": round(dur_sim, 1) if dur_sim is not None else None,
        "confidence": confidence,
        "musicbrainz_url": f"https://musicbrainz.org/recording/{recording.get('id')}" if recording.get("id") else None,
    }


def find_best_match(track) -> Optional[dict]:
    """
    Searches MusicBrainz using tag data first, falling back to the
    filename-derived guess if tags are missing, and returns the
    highest-confidence candidate (or None if nothing usable was found).
    """
    artist = track.tag_artist or track.guess_artist
    title = track.tag_title or track.guess_title

    if not title:
        return None

    candidates = _mb_search(artist, title, limit=5)
    if not candidates and artist:
        # Retry title-only in case the artist tag is wrong/misspelled
        candidates = _mb_search(None, title, limit=5)

    if not candidates:
        return None

    scored = [score_candidate(track, c) for c in candidates]
    scored.sort(key=lambda s: s["confidence"], reverse=True)
    return scored[0]


def match_tracks(tracks: list, progress_callback=None) -> None:
    """Mutates each track in-place, setting track.match to the best candidate dict."""
    for i, track in enumerate(tracks):
        track.match = find_best_match(track)
        if progress_callback:
            progress_callback(i + 1, len(tracks))
