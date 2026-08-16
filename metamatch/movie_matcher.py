"""
movie_matcher.py
Queries The Movie Database (TMDB) for candidate movies and scores each
one against the local file's title/year (from container tags or, more
often, the parsed filename) to find the best match with a 0-100
confidence score. Requires a free TMDB API key (see core/config.py).
"""

from __future__ import annotations

import time
import threading
from typing import Optional

import requests
from rapidfuzz import fuzz

from core.config import get_tmdb_api_key

TMDB_SEARCH_URL = "https://api.themoviedb.org/3/search/movie"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p"

_last_request_lock = threading.Lock()
_last_request_time = 0.0
_MIN_INTERVAL = 0.05  # TMDB's limits are generous; a light throttle is plenty


def _throttle():
    global _last_request_time
    with _last_request_lock:
        elapsed = time.time() - _last_request_time
        if elapsed < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - elapsed)
        _last_request_time = time.time()


class TmdbNotConfigured(Exception):
    pass


def _tmdb_search(title: str, year: Optional[str], limit: int = 5) -> list[dict]:
    api_key = get_tmdb_api_key()
    if not api_key:
        raise TmdbNotConfigured("No TMDB API key configured.")
    if not title:
        return []

    params = {"api_key": api_key, "query": title, "include_adult": "false"}
    if year:
        params["primary_release_year"] = year

    _throttle()
    try:
        resp = requests.get(TMDB_SEARCH_URL, params=params, timeout=10)
        if resp.status_code == 401:
            raise TmdbNotConfigured("TMDB rejected the API key - check it's correct.")
        resp.raise_for_status()
        return (resp.json().get("results") or [])[:limit]
    except requests.RequestException:
        return []


def _year_score(local_year: Optional[str], candidate_date: Optional[str]) -> Optional[float]:
    if not local_year or not candidate_date:
        return None
    try:
        local_y = int(local_year)
        cand_y = int(str(candidate_date)[:4])
    except (TypeError, ValueError):
        return None
    diff = abs(local_y - cand_y)
    if diff == 0:
        return 100.0
    if diff == 1:
        return 60.0
    if diff == 2:
        return 25.0
    return 0.0


def score_candidate(video, candidate: dict) -> dict:
    query_title = video.tag_title or video.guess_title or video.filename
    query_year = video.tag_year or video.guess_year

    cand_title = candidate.get("title") or ""
    cand_original_title = candidate.get("original_title") or ""
    cand_date = candidate.get("release_date") or None
    cand_year = cand_date[:4] if cand_date else None

    title_sim = max(
        fuzz.token_sort_ratio(query_title, cand_title) if cand_title else 0,
        fuzz.token_sort_ratio(query_title, cand_original_title) if cand_original_title else 0,
    )
    year_sim = _year_score(query_year, cand_date)
    vote_score = min(100.0, float(candidate.get("vote_average") or 0) * 10)

    weighted_sum = title_sim * 0.55
    total_weight = 0.55

    if year_sim is not None:
        weighted_sum += year_sim * 0.30
        total_weight += 0.30

    weighted_sum += vote_score * 0.15
    total_weight += 0.15

    confidence = round(weighted_sum / total_weight, 1) if total_weight else 0.0

    poster_path = candidate.get("poster_path")
    return {
        "tmdb_id": candidate.get("id"),
        "title": cand_title,
        "original_title": cand_original_title if cand_original_title != cand_title else None,
        "year": cand_year,
        "release_date": cand_date,
        "overview": candidate.get("overview"),
        "vote_average": candidate.get("vote_average"),
        "poster_path": poster_path,
        "poster_url": f"{TMDB_IMAGE_BASE}/w342{poster_path}" if poster_path else None,
        "poster_url_full": f"{TMDB_IMAGE_BASE}/original{poster_path}" if poster_path else None,
        "title_similarity": round(title_sim, 1),
        "year_similarity": round(year_sim, 1) if year_sim is not None else None,
        "confidence": confidence,
        "tmdb_url": f"https://www.themoviedb.org/movie/{candidate.get('id')}" if candidate.get("id") else None,
    }


def find_best_match(video) -> Optional[dict]:
    title = video.tag_title or video.guess_title
    year = video.tag_year or video.guess_year
    if not title:
        return None

    candidates = _tmdb_search(title, year, limit=5)
    if not candidates and year:
        # Retry without the year filter in case our year guess was wrong
        candidates = _tmdb_search(title, None, limit=5)
    if not candidates:
        return None

    scored = [score_candidate(video, c) for c in candidates]
    scored.sort(key=lambda s: s["confidence"], reverse=True)
    return scored[0]


def match_videos(videos: list, progress_callback=None) -> None:
    for i, video in enumerate(videos):
        try:
            video.match = find_best_match(video)
        except TmdbNotConfigured:
            video.match = None
            if progress_callback:
                progress_callback(i + 1, len(videos))
            raise
        if progress_callback:
            progress_callback(i + 1, len(videos))
