"""
episode_scanner.py
Walks a folder for TV episode files and works out, for each one, which
series it belongs to and which season/episode it is.

TV is the messiest of the three media types to parse: unlike a movie
(one title + year) an episode carries a series name, a season number, an
episode number (sometimes several, for two-parters muxed into one file),
and often an episode title - and almost none of that is in the container
tags, so it nearly all comes from the filename. This module handles the
common real-world naming conventions:

    Show.Name.S01E02.Episode.Title.1080p.WEB-DL.mkv
    Show Name - 1x02 - Episode Title.mkv
    Show.Name.S01E02E03.Two.Parter.mkv        (multi-episode)
    Show Name/Season 01/Show Name - S01E02.mkv (season subfolder)

It deliberately reuses video_scanner's scene-junk stripping so the series
name that falls out is as clean as the movie titles do. A file with no
recognisable SxxEyy / NxNN marker is returned unparsed (season/episode
None) - the matcher then simply can't match it as an episode, rather than
this module guessing wildly.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

from .fingerprint import content_fingerprint
from .video_scanner import (
    SUPPORTED_EXTENSIONS, FFPROBE_AVAILABLE, _ffprobe,
    _JUNK_RE, _TRAILING_GROUP_RE,
)

# S01E02, s1e2, S01E02E03, S01E02-E03, S01.E02, 1x02, etc. The season/first-
# episode groups are always captured; extra episodes (multi-part files) are
# captured as one trailing blob and split out afterwards.
_SXXEYY_RE = re.compile(
    r"[Ss](\d{1,2})[\s._-]*[Ee](\d{1,3})((?:[\s._-]*[Ee]\d{1,3})*)",
)
_NXNN_RE = re.compile(r"(?<!\d)(\d{1,2})x(\d{1,3})(?!\d)")
_EXTRA_EP_RE = re.compile(r"[Ee](\d{1,3})")
# A "Season 01" / "S01" parent-folder name, used both to pull a season number
# when the filename lacks one and to recognise (and skip) a season subfolder
# when climbing the tree for the series name.
_SEASON_FOLDER_RE = re.compile(r"^(?:season|series|s)[\s._-]*(\d{1,2})$", re.IGNORECASE)
# Last-resort episode-only naming ("E07.mkv", "Episode 7.mkv") - only trusted
# when a season subfolder supplies the season number, so a stray number in a
# normal filename can't be misread as an episode.
_EP_ONLY_RE = re.compile(r"^(?:e|ep|episode)[\s._-]*(\d{1,3})$", re.IGNORECASE)


@dataclass
class EpisodeFile:
    path: str
    filename: str
    ext: str
    size_bytes: int
    duration_seconds: Optional[float] = None
    mtime_ns: Optional[int] = None
    content_hash: Optional[str] = None

    # Parsed from the filename (occasionally helped by the parent folder).
    series_guess: Optional[str] = None
    season: Optional[int] = None
    episode: Optional[int] = None            # primary (first) episode number
    episodes: list[int] = field(default_factory=list)  # all, for multi-part files
    episode_title_guess: Optional[str] = None

    match: Optional[dict] = field(default=None)

    @property
    def id(self) -> str:
        return self.path

    @property
    def parsed(self) -> bool:
        """True if we recovered enough to even attempt a match."""
        return bool(self.series_guess and self.season is not None and self.episode is not None)

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "path": self.path,
            "filename": self.filename,
            "ext": self.ext,
            "size_bytes": self.size_bytes,
            "duration_seconds": self.duration_seconds,
            "series_guess": self.series_guess,
            "season": self.season,
            "episode": self.episode,
            "episodes": self.episodes,
            "episode_title_guess": self.episode_title_guess,
            "parsed": self.parsed,
        }
        if self.match:
            d["match"] = self.match
        return d


def _clean_series_name(raw: str) -> Optional[str]:
    working = _JUNK_RE.sub(" ", raw)
    working = working.replace(".", " ").replace("_", " ")
    working = _TRAILING_GROUP_RE.sub("", working)
    working = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", working)
    # A trailing year like "Show Name 2019" is part of the series name for
    # disambiguation, so it's kept - unlike the scene junk stripped above.
    working = re.sub(r"\s+", " ", working).strip(" -._")
    return working or None


def _clean_episode_title(raw: str) -> Optional[str]:
    working = _JUNK_RE.sub(" ", raw)
    working = working.replace(".", " ").replace("_", " ")
    working = _TRAILING_GROUP_RE.sub("", working)
    working = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", working)
    working = re.sub(r"\s+", " ", working).strip(" -._")
    # Guard against a leftover junk fragment masquerading as a title.
    if not working or len(working) < 2:
        return None
    return working


def _series_from_folders(parent_dir: str) -> Optional[str]:
    """Walk up from the file's folder, skipping any 'Season NN'/'SNN'
    subfolder, and clean the first real ancestor as the series name -
    so an episode inside '<Show>/Season 01/' is credited to <Show>, not
    to the season folder."""
    d = parent_dir
    for _ in range(3):  # look at most a few levels up
        if not d:
            break
        base = os.path.basename(d)
        if base and not _SEASON_FOLDER_RE.match(base):
            cleaned = _clean_series_name(base)
            if cleaned:
                return cleaned
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


def _parse_filename(filename: str, parent_dir: str = "") -> dict:
    stem, _ = os.path.splitext(filename)

    season = episode = None
    episodes: list[int] = []
    before = stem
    after = ""

    m = _SXXEYY_RE.search(stem)
    if m:
        season = int(m.group(1))
        episode = int(m.group(2))
        episodes = [episode] + [int(e) for e in _EXTRA_EP_RE.findall(m.group(3) or "")]
        before, after = stem[:m.start()], stem[m.end():]
    else:
        m = _NXNN_RE.search(stem)
        if m:
            season = int(m.group(1))
            episode = int(m.group(2))
            episodes = [episode]
            before, after = stem[:m.start()], stem[m.end():]
        else:
            # Episode-only naming, trusted only alongside a season subfolder.
            em = _EP_ONLY_RE.match(stem.strip())
            season_folder = _SEASON_FOLDER_RE.match(os.path.basename(parent_dir)) if parent_dir else None
            if em and season_folder:
                season = int(season_folder.group(1))
                episode = int(em.group(1))
                episodes = [episode]
                before, after = "", ""

    # If we got an episode but no season (rare "E02"-only naming), try the
    # parent folder ("Season 1") as a fallback.
    if episode is not None and season is None and parent_dir:
        fm = _SEASON_FOLDER_RE.match(os.path.basename(parent_dir))
        if fm:
            season = int(fm.group(1))

    series = _clean_series_name(before)
    # If the part before the marker was empty or was itself a season folder,
    # climb the folder tree (past 'Season NN') for the real series name.
    if not series or _SEASON_FOLDER_RE.match(series or ""):
        series = _series_from_folders(parent_dir) or series

    title = _clean_episode_title(after) if after else None

    return {
        "series_guess": series,
        "season": season,
        "episode": episode,
        "episodes": episodes,
        "episode_title_guess": title,
    }


def read_episode(path: str) -> EpisodeFile:
    filename = os.path.basename(path)
    ext = os.path.splitext(filename)[1].lower()
    stat_result = os.stat(path)

    parsed = _parse_filename(filename, parent_dir=os.path.dirname(path))

    fmt = _ffprobe(path)
    duration = None
    if fmt.get("duration"):
        try:
            duration = float(fmt["duration"])
        except (TypeError, ValueError):
            duration = None

    return EpisodeFile(
        path=path, filename=filename, ext=ext, size_bytes=stat_result.st_size,
        duration_seconds=duration, mtime_ns=stat_result.st_mtime_ns,
        content_hash=content_fingerprint(path),
        **parsed,
    )


def scan_folder(folder: str, recursive: bool = True) -> list[EpisodeFile]:
    if not os.path.isdir(folder):
        raise NotADirectoryError(f"Not a folder: {folder}")

    from .dedup import QUARANTINE_DIRNAME

    results: list[EpisodeFile] = []
    walker = os.walk(folder) if recursive else [(folder, [], os.listdir(folder))]

    for root, dirs, files in walker:
        dirs[:] = [d for d in dirs if d != QUARANTINE_DIRNAME]
        for name in files:
            if os.path.splitext(name)[1].lower() in SUPPORTED_EXTENSIONS:
                full_path = os.path.join(root, name)
                try:
                    results.append(read_episode(full_path))
                except Exception:
                    continue
    results.sort(key=lambda e: e.path.lower())
    return results
