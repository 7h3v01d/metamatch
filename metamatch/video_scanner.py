"""
video_scanner.py
Walks a folder for video files. Unlike audio (where mutagen gives us
clean per-format tag readers), embedded metadata in video containers is
inconsistent and format-specific, so this uses ffprobe (part of ffmpeg,
a very common system dependency) to pull whatever container-level title/
date tags and duration exist, uniformly across mp4/mkv/avi/mov/wmv.

Most downloaded/ripped movie files don't have useful embedded tags at
all, so filename parsing carries more of the weight here than it does
for music: this strips scene-release junk (resolution, source, codec,
release-group suffixes) and pulls out a title + year.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Optional

from .fingerprint import content_fingerprint

SUPPORTED_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".m4v"}

FFPROBE_AVAILABLE = shutil.which("ffprobe") is not None

# Tokens that show up in scene-release filenames and should be stripped
# before we try to read what's left as a movie title.
_JUNK_TOKENS = [
    r"2160p", r"1080p", r"720p", r"480p", r"4k", r"uhd",
    r"blu[\-\s]?ray", r"bdrip", r"brrip", r"web[\-\s]?rip", r"web[\-\s]?dl", r"webdl",
    r"hdrip", r"dvdrip", r"dvdscr", r"hdcam", r"hdts", r"cam\b",
    r"x264", r"x265", r"h\.?264", r"h\.?265", r"hevc", r"avc",
    r"aac(?:\d\.\d)?", r"ac3", r"dts(?:-hd)?", r"dd5\.1", r"5\.1", r"7\.1",
    r"remastered", r"extended", r"unrated", r"directors?[\.\s]?cut", r"theatrical",
    r"proper", r"repack", r"multi", r"dual[\-\s]?audio",
    r"yify", r"rarbg", r"eztv", r"ettv",
]
_JUNK_RE = re.compile(r"\b(" + "|".join(_JUNK_TOKENS) + r")\b", re.IGNORECASE)
_YEAR_RE = re.compile(r"(?:^|[\(\[\s\.])(19\d{2}|20\d{2})(?:$|[\)\]\s\.])")
_TRAILING_GROUP_RE = re.compile(r"-[A-Za-z0-9]+$")


@dataclass
class VideoFile:
    path: str
    filename: str
    ext: str
    size_bytes: int
    duration_seconds: Optional[float] = None
    mtime_ns: Optional[int] = None
    content_hash: Optional[str] = None

    tag_title: Optional[str] = None
    tag_year: Optional[str] = None

    guess_title: Optional[str] = None
    guess_year: Optional[str] = None

    match: Optional[dict] = field(default=None)

    @property
    def id(self) -> str:
        return self.path

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "path": self.path,
            "filename": self.filename,
            "ext": self.ext,
            "size_bytes": self.size_bytes,
            "duration_seconds": self.duration_seconds,
            "tag_title": self.tag_title,
            "tag_year": self.tag_year,
            "guess_title": self.guess_title,
            "guess_year": self.guess_year,
        }
        if self.match:
            d["match"] = self.match
        return d


def _guess_title_year_from_filename(filename: str) -> tuple[Optional[str], Optional[str]]:
    stem, _ = os.path.splitext(filename)

    # Strip junk tokens first, while dots are still intact - some tokens
    # rely on a literal dot (h.264, 5.1) that a later dot->space pass would
    # otherwise split apart before the junk regex ever sees them.
    working = _JUNK_RE.sub(" ", stem)
    working = working.replace(".", " ").replace("_", " ")
    working = re.sub(r"\s+", " ", working).strip()

    year = None
    title_part = working

    year_match = _YEAR_RE.search(working)
    if year_match:
        year = year_match.group(1)
        title_part = working[:year_match.start()]

    # Strip a trailing "-GROUP" release-tag if present, then sweep for any
    # remaining junk tokens that only became isolated after the dot/underscore pass.
    title_part = _TRAILING_GROUP_RE.sub("", title_part)
    title_part = _JUNK_RE.sub(" ", title_part)
    title_part = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", title_part)
    title_part = re.sub(r"\s+", " ", title_part).strip(" -._")

    return (title_part or None), year


def _ffprobe(path: str) -> dict:
    if not FFPROBE_AVAILABLE:
        return {}
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", path],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20, check=False,
        )
        data = json.loads(proc.stdout or b"{}")
        return data.get("format", {}) or {}
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return {}


def read_video(path: str) -> VideoFile:
    filename = os.path.basename(path)
    ext = os.path.splitext(filename)[1].lower()
    stat_result = os.stat(path)
    size_bytes = stat_result.st_size
    mtime_ns = stat_result.st_mtime_ns
    # Sampled (not full) hash for video - see fingerprint.py. A multi-
    # gigabyte movie only gets three bounded 1 MiB reads, not a full read.
    content_hash = content_fingerprint(path)

    fmt = _ffprobe(path)
    duration = None
    if fmt.get("duration"):
        try:
            duration = float(fmt["duration"])
        except (TypeError, ValueError):
            duration = None

    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    tag_title = tags.get("title")
    tag_date = tags.get("date") or tags.get("year")
    tag_year = None
    if tag_date:
        year_match = re.search(r"(19\d{2}|20\d{2})", str(tag_date))
        tag_year = year_match.group(1) if year_match else None

    guess_title, guess_year = _guess_title_year_from_filename(filename)

    return VideoFile(
        path=path, filename=filename, ext=ext, size_bytes=size_bytes,
        duration_seconds=duration, mtime_ns=mtime_ns, content_hash=content_hash,
        tag_title=tag_title, tag_year=tag_year,
        guess_title=guess_title, guess_year=guess_year,
    )


def scan_folder(folder: str, recursive: bool = True) -> list[VideoFile]:
    if not os.path.isdir(folder):
        raise NotADirectoryError(f"Not a folder: {folder}")

    from .dedup import QUARANTINE_DIRNAME

    results: list[VideoFile] = []
    walker = os.walk(folder) if recursive else [(folder, [], os.listdir(folder))]

    for root, dirs, files in walker:
        # Don't walk into our own quarantine folder - see the matching
        # comment in scanner.py for why.
        dirs[:] = [d for d in dirs if d != QUARANTINE_DIRNAME]
        for name in files:
            ext = os.path.splitext(name)[1].lower()
            if ext in SUPPORTED_EXTENSIONS:
                full_path = os.path.join(root, name)
                try:
                    results.append(read_video(full_path))
                except Exception:
                    continue
    results.sort(key=lambda v: v.path.lower())
    return results
