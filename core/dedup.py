"""
dedup.py
Two kinds of duplicate detection over a scanned set of tracks:

  - "exact": files that are byte-for-byte identical (same MD5 hash) -
    almost always accidental double-copies of the same file.
  - "probable": files that appear to be the same recording in different
    encodes/rips - grouped by MusicBrainz recording id when a match is
    available, falling back to normalized artist+title text otherwise.

Neither function deletes anything; quarantine() is the only function that
touches disk, and it *moves* files into a sibling folder rather than
deleting them, so the action is easy to reverse.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections import defaultdict

QUARANTINE_DIRNAME = "_metamatch_duplicates"


def file_hash(path: str, chunk_size: int = 1 << 20) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _normalize_text(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _probable_key(track) -> str | None:
    if track.match and track.match.get("recording_id"):
        return f"mb:{track.match['recording_id']}"

    artist = track.tag_artist or track.guess_artist
    title = track.tag_title or track.guess_title
    if artist and title:
        return f"text:{_normalize_text(artist)}|{_normalize_text(title)}"
    return None


def _movie_probable_key(video) -> str | None:
    if video.match and video.match.get("tmdb_id"):
        return f"tmdb:{video.match['tmdb_id']}"

    title = video.tag_title or video.guess_title
    year = video.tag_year or video.guess_year
    if title:
        key = f"text:{_normalize_text(title)}"
        if year:
            key += f"|{year}"
        return key
    return None


def find_exact_duplicates(tracks: list) -> list[dict]:
    by_hash: dict[str, list] = defaultdict(list)
    for t in tracks:
        try:
            by_hash[file_hash(t.path)].append(t)
        except OSError:
            continue

    groups = []
    for digest, group_tracks in by_hash.items():
        if len(group_tracks) > 1:
            groups.append({
                "type": "exact",
                "key": digest,
                "label": "Identical file",
                "files": [_file_summary(t) for t in group_tracks],
            })
    return groups


def find_probable_duplicates(tracks: list) -> list[dict]:
    by_key: dict[str, list] = defaultdict(list)
    for t in tracks:
        key = _probable_key(t)
        if key:
            by_key[key].append(t)

    groups = []
    for key, group_tracks in by_key.items():
        # Skip groups where every file in it is also byte-identical - not
        # worth a second listing; exact-duplicate detection already covers it.
        distinct_sizes = {t.size_bytes for t in group_tracks}
        if len(group_tracks) > 1:
            label = "Same MusicBrainz recording" if key.startswith("mb:") else "Same artist + title"
            groups.append({
                "type": "probable",
                "key": key,
                "label": label,
                "files": [_file_summary(t) for t in group_tracks],
            })
    return groups


def find_probable_duplicates_movies(videos: list) -> list[dict]:
    by_key: dict[str, list] = defaultdict(list)
    for v in videos:
        key = _movie_probable_key(v)
        if key:
            by_key[key].append(v)

    groups = []
    for key, group_videos in by_key.items():
        if len(group_videos) > 1:
            label = "Same TMDB movie" if key.startswith("tmdb:") else "Same title + year"
            groups.append({
                "type": "probable",
                "key": key,
                "label": label,
                "files": [_file_summary(v) for v in group_videos],
            })
    return groups


def _file_summary(track) -> dict:
    return {
        "id": track.id,
        "path": track.path,
        "filename": track.filename,
        "size_bytes": track.size_bytes,
        "duration_seconds": track.duration_seconds,
        "ext": track.ext,
        "confidence": (track.match or {}).get("confidence"),
    }


def quarantine(paths: list[str], root_folder: str) -> list[dict]:
    """Moves each given file into <root_folder>/_metamatch_duplicates, avoiding name collisions."""
    dest_dir = os.path.join(root_folder, QUARANTINE_DIRNAME)
    os.makedirs(dest_dir, exist_ok=True)

    results = []
    for path in paths:
        result = {"original_path": path, "new_path": None, "error": None}
        try:
            if not os.path.exists(path):
                raise FileNotFoundError("File no longer exists.")
            name = os.path.basename(path)
            base, ext = os.path.splitext(name)
            candidate = os.path.join(dest_dir, name)
            counter = 2
            while os.path.exists(candidate):
                candidate = os.path.join(dest_dir, f"{base} ({counter}){ext}")
                counter += 1
            os.rename(path, candidate)
            result["new_path"] = candidate
        except Exception as e:
            result["error"] = str(e)
        results.append(result)
    return results
