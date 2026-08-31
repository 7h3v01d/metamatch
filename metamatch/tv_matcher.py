"""
tv_matcher.py
Matches a local episode file against The Movie Database (TMDB). Two-step,
unlike movies: first identify the *series* (search/tv by the parsed show
name), then fetch the specific *episode* (tv/{id}/season/{s}/episode/{e})
to pull its real title, air date, overview and still image.

Confidence is about series identity plus whether the claimed episode
actually exists on that series: a great series-name match to a show that
has no S3E14 is not a good match for a file claiming to be S3E14, so a
missing episode is penalised rather than silently accepted.

Shares TMDB config, throttling and the TmdbNotConfigured signal with the
movie matcher.
"""

from __future__ import annotations

from typing import Optional

import requests
from rapidfuzz import fuzz

from . import scoring
from .config import get_tmdb_api_key
from .movie_matcher import TmdbNotConfigured, _throttle, TMDB_IMAGE_BASE

TMDB_TV_SEARCH_URL = "https://api.themoviedb.org/3/search/tv"
TMDB_TV_EPISODE_URL = "https://api.themoviedb.org/3/tv/{series_id}/season/{season}/episode/{episode}"


def _tmdb_search_series(series: str, limit: int = 5) -> list[dict]:
    api_key = get_tmdb_api_key()
    if not api_key:
        raise TmdbNotConfigured("No TMDB API key configured.")
    if not series:
        return []
    _throttle()
    try:
        resp = requests.get(
            TMDB_TV_SEARCH_URL,
            params={"api_key": api_key, "query": series, "include_adult": "false"},
            timeout=10,
        )
        if resp.status_code == 401:
            raise TmdbNotConfigured("TMDB rejected the API key - check it's correct.")
        resp.raise_for_status()
        return (resp.json().get("results") or [])[:limit]
    except requests.RequestException:
        return []


def _tmdb_episode(series_id: int, season: int, episode: int) -> Optional[dict]:
    api_key = get_tmdb_api_key()
    if not api_key:
        raise TmdbNotConfigured("No TMDB API key configured.")
    _throttle()
    try:
        resp = requests.get(
            TMDB_TV_EPISODE_URL.format(series_id=series_id, season=season, episode=episode),
            params={"api_key": api_key},
            timeout=10,
        )
        if resp.status_code == 404:
            return None  # series exists but has no such episode
        if resp.status_code == 401:
            raise TmdbNotConfigured("TMDB rejected the API key - check it's correct.")
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException:
        return None


def _series_rank_score(rank: Optional[int]) -> Optional[float]:
    if rank is None:
        return None
    return max(0.0, 100.0 - rank * 25.0)


def score_series(episode_file, candidate: dict, rank: Optional[int] = None) -> dict:
    query = episode_file.series_guess or ""
    cand_name = candidate.get("name") or ""
    cand_original = candidate.get("original_name") or ""
    first_air = candidate.get("first_air_date") or None

    name_sim = max(
        fuzz.token_sort_ratio(query, cand_name) if cand_name else 0,
        fuzz.token_sort_ratio(query, cand_original) if cand_original else 0,
    )
    rank_sim = _series_rank_score(rank)

    weighted_sum = name_sim * 0.8
    total_weight = 0.8
    if rank_sim is not None:
        weighted_sum += rank_sim * 0.2
        total_weight += 0.2
    confidence = round(weighted_sum / total_weight, 1) if total_weight else 0.0

    return {
        "series_tmdb_id": candidate.get("id"),
        "series_name": cand_name,
        "series_first_air_date": first_air,
        "series_year": first_air[:4] if first_air else None,
        "name_similarity": round(name_sim, 1),
        "search_rank": rank,
        "series_confidence": confidence,
    }


def _build_match(episode_file, series: dict, ep: dict) -> dict:
    still = ep.get("still_path")
    season = episode_file.season
    episode = episode_file.episode
    return {
        "type": "tv",
        "series_tmdb_id": series["series_tmdb_id"],
        "series_name": series["series_name"],
        "series_year": series.get("series_year"),
        "season": season,
        "episode": episode,
        "episodes": episode_file.episodes or [episode],
        "episode_title": ep.get("name"),
        "episode_overview": ep.get("overview"),
        "air_date": ep.get("air_date"),
        "vote_average": ep.get("vote_average"),
        "still_path": still,
        "still_url": f"{TMDB_IMAGE_BASE}/w300{still}" if still else None,
        "still_url_full": f"{TMDB_IMAGE_BASE}/original{still}" if still else None,
        "name_similarity": series["name_similarity"],
        # Episode existence is strong identity evidence; fold it into the
        # headline confidence so "great series name, wrong episode number"
        # doesn't score as a confident match.
        "confidence": series["series_confidence"],
        "tmdb_url": (
            f"https://www.themoviedb.org/tv/{series['series_tmdb_id']}/"
            f"season/{season}/episode/{episode}"
        ),
    }


def find_best_match(episode_file) -> Optional[dict]:
    if not episode_file.parsed:
        return None

    candidates = _tmdb_search_series(episode_file.series_guess, limit=5)
    if not candidates:
        return None

    scored = [score_series(episode_file, c, rank=i) for i, c in enumerate(candidates)]
    scored.sort(key=lambda s: s["series_confidence"], reverse=True)

    # Series identity is the ambiguity that matters for TV - "which show is
    # this" - so the margin/runner-up describe the series contest.
    def _annotated(match):
        return scoring.annotate_winner(
            scored, match, confidence_key="series_confidence",
            label_fields=("series_name", "series_year"))

    # Walk candidates best-first, taking the first whose claimed episode
    # actually exists. If none do, fall back to the top series with a
    # confidence penalty (we found the show but not the episode).
    for series in scored:
        ep = _tmdb_episode(series["series_tmdb_id"], episode_file.season, episode_file.episode)
        if ep is not None:
            return _annotated(_build_match(episode_file, series, ep))

    top = scored[0]
    fallback = _build_match(episode_file, top, {})
    fallback["episode_title"] = None
    fallback["confidence"] = round(top["series_confidence"] * 0.5, 1)
    fallback["episode_missing"] = True
    return _annotated(fallback)


def match_episodes(episodes: list, progress_callback=None) -> None:
    for i, ep_file in enumerate(episodes):
        try:
            ep_file.match = find_best_match(ep_file)
        except TmdbNotConfigured:
            ep_file.match = None
            if progress_callback:
                progress_callback(i + 1, len(episodes))
            raise
        if progress_callback:
            progress_callback(i + 1, len(episodes))


TMDB_TV_DETAILS_URL = "https://api.themoviedb.org/3/tv/{series_id}"
TMDB_TV_SEASON_URL = "https://api.themoviedb.org/3/tv/{series_id}/season/{season}"


def fetch_series_details(series_id: int) -> Optional[dict]:
    """Series-level metadata for a tvshow.nfo + series poster: title, plot,
    first-air year, genres, rating, and the poster path. Returns None on any
    network/HTTP failure (callers treat series metadata as best-effort)."""
    api_key = get_tmdb_api_key()
    if not api_key:
        raise TmdbNotConfigured("No TMDB API key configured.")
    _throttle()
    try:
        resp = requests.get(TMDB_TV_DETAILS_URL.format(series_id=series_id),
                            params={"api_key": api_key}, timeout=10)
        if resp.status_code == 401:
            raise TmdbNotConfigured("TMDB rejected the API key - check it's correct.")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException:
        return None

    poster = data.get("poster_path")
    first_air = data.get("first_air_date") or None
    return {
        "series_tmdb_id": series_id,
        "name": data.get("name"),
        "overview": data.get("overview"),
        "first_air_date": first_air,
        "year": first_air[:4] if first_air else None,
        "genres": [g.get("name") for g in (data.get("genres") or []) if g.get("name")],
        "vote_average": data.get("vote_average"),
        "status": data.get("status"),
        "poster_path": poster,
        "poster_url_full": f"{TMDB_IMAGE_BASE}/original{poster}" if poster else None,
    }


def fetch_season_poster_url(series_id: int, season: int) -> Optional[str]:
    """Full-size poster URL for one season, or None if the season has no
    dedicated poster (or the lookup fails)."""
    api_key = get_tmdb_api_key()
    if not api_key:
        raise TmdbNotConfigured("No TMDB API key configured.")
    _throttle()
    try:
        resp = requests.get(TMDB_TV_SEASON_URL.format(series_id=series_id, season=season),
                            params={"api_key": api_key}, timeout=10)
        if resp.status_code in (401, 404):
            return None
        resp.raise_for_status()
        poster = resp.json().get("poster_path")
        return f"{TMDB_IMAGE_BASE}/original{poster}" if poster else None
    except requests.RequestException:
        return None
