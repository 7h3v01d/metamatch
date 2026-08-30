"""
scanner.py
Walks a folder for audio files and reads whatever tag data is already
embedded, plus filesystem-derived hints (filename, folder name) that are
used as a fallback when tags are missing or unreliable.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

from mutagen import File as MutagenFile
from mutagen.id3 import ID3
from mutagen.easyid3 import EasyID3
from mutagen.asf import ASF

from .fingerprint import content_fingerprint

SUPPORTED_EXTENSIONS = {".mp3", ".wma", ".flac", ".m4a", ".ogg", ".wav"}

# Common "junk" tokens that show up in ripped/downloaded filenames and hurt
# fuzzy matching if left in, e.g. "01 - Artist - Title (Official Audio).mp3"
_JUNK_PATTERNS = [
    r"\(official.*?\)", r"\[official.*?\]",
    r"\(lyrics?.*?\)", r"\[lyrics?.*?\]",
    r"\(audio\)", r"\[audio\]",
    r"\(hq\)", r"\[hq\]",
    r"\(hd\)", r"\[hd\]",
    r"\(remaster(ed)?.*?\)", r"\[remaster(ed)?.*?\]",
    r"^\d{1,3}[\s._-]+",  # leading track numbers
    r"\.mp3$|\.wma$|\.flac$|\.m4a$|\.ogg$|\.wav$",
]


@dataclass
class TrackFile:
    path: str
    filename: str
    ext: str
    size_bytes: int
    duration_seconds: Optional[float] = None
    mtime_ns: Optional[int] = None
    content_hash: Optional[str] = None

    tag_artist: Optional[str] = None
    tag_title: Optional[str] = None
    tag_album: Optional[str] = None
    tag_track_number: Optional[str] = None
    tag_year: Optional[str] = None

    # Best-effort guesses parsed from the filename, used when tags are
    # missing or when we want a second signal to cross-check tags against.
    guess_artist: Optional[str] = None
    guess_title: Optional[str] = None

    match: Optional[dict] = field(default=None)  # filled in by matcher.py

    @property
    def id(self) -> str:
        return self.path

    @property
    def has_usable_tags(self) -> bool:
        return bool(self.tag_artist and self.tag_title)

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "path": self.path,
            "filename": self.filename,
            "ext": self.ext,
            "size_bytes": self.size_bytes,
            "duration_seconds": self.duration_seconds,
            "tag_artist": self.tag_artist,
            "tag_title": self.tag_title,
            "tag_album": self.tag_album,
            "tag_track_number": self.tag_track_number,
            "tag_year": self.tag_year,
            "guess_artist": self.guess_artist,
            "guess_title": self.guess_title,
        }
        if self.match:
            d["match"] = self.match
        return d


def _clean_filename_stem(stem: str) -> str:
    cleaned = stem
    for pat in _JUNK_PATTERNS:
        cleaned = re.sub(pat, " ", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.replace("_", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -._")
    return cleaned


def _guess_artist_title_from_filename(filename: str) -> tuple[Optional[str], Optional[str]]:
    stem, _ = os.path.splitext(filename)
    stem = _clean_filename_stem(stem)
    # Most common convention: "Artist - Title"
    for sep in (" - ", " – ", " — "):
        if sep in stem:
            parts = stem.split(sep, 1)
            if len(parts) == 2 and all(p.strip() for p in parts):
                return parts[0].strip(), parts[1].strip()
    return None, stem or None


def _read_mp3_tags(path: str) -> dict:
    tags = {}
    try:
        easy = EasyID3(path)
        tags["artist"] = (easy.get("artist") or [None])[0]
        tags["title"] = (easy.get("title") or [None])[0]
        tags["album"] = (easy.get("album") or [None])[0]
        tags["tracknumber"] = (easy.get("tracknumber") or [None])[0]
        tags["date"] = (easy.get("date") or [None])[0]
    except Exception:
        pass
    return tags


def _read_wma_tags(path: str) -> dict:
    tags = {}
    try:
        audio = ASF(path)

        def first(key):
            vals = audio.tags.get(key) if audio.tags else None
            return str(vals[0]) if vals else None

        tags["artist"] = first("Author") or first("WM/AlbumArtist")
        tags["title"] = first("Title")
        tags["album"] = first("WM/AlbumTitle")
        tags["tracknumber"] = first("WM/TrackNumber")
        tags["date"] = first("WM/Year")
    except Exception:
        pass
    return tags


def _read_wav_tags(path: str) -> dict:
    """WAVE embeds real ID3v2 frames in a RIFF chunk - mutagen.File(easy=True)
    doesn't understand that (it returns a WAVE object with no easy-tag
    interface, tags silently read as empty), so this reads the raw ID3
    frames directly instead, the same way _read_mp3_tags does under the
    hood via EasyID3."""
    tags = {}
    try:
        from mutagen.wave import WAVE
        audio = WAVE(path)
        if audio.tags:
            def first(frame_id):
                frame = audio.tags.get(frame_id)
                return str(frame.text[0]) if frame and frame.text else None

            tags["artist"] = first("TPE1")
            tags["title"] = first("TIT2")
            tags["album"] = first("TALB")
            tags["tracknumber"] = first("TRCK")
            tags["date"] = first("TDRC")
    except Exception:
        pass
    return tags


def _read_generic_tags(path: str) -> dict:
    """Fallback for flac/m4a/ogg using mutagen's generic File + EasyTags-like access."""
    tags = {}
    try:
        audio = MutagenFile(path, easy=True)
        if audio and audio.tags:
            tags["artist"] = (audio.tags.get("artist") or [None])[0]
            tags["title"] = (audio.tags.get("title") or [None])[0]
            tags["album"] = (audio.tags.get("album") or [None])[0]
            tags["tracknumber"] = (audio.tags.get("tracknumber") or [None])[0]
            tags["date"] = (audio.tags.get("date") or [None])[0]
    except Exception:
        pass
    return tags


def read_track(path: str) -> TrackFile:
    filename = os.path.basename(path)
    ext = os.path.splitext(filename)[1].lower()
    stat_result = os.stat(path)
    size_bytes = stat_result.st_size
    mtime_ns = stat_result.st_mtime_ns
    # Bounded-cost hash (whole file if small, sampled if large - see
    # fingerprint.py) so a later apply/quarantine/undo can detect a file
    # whose content changed even if its size and modification time were
    # both preserved (e.g. a restore tool that keeps mtimes, or tampering
    # that specifically avoids touching either).
    content_hash = content_fingerprint(path)

    duration = None
    try:
        audio = MutagenFile(path)
        if audio and audio.info and hasattr(audio.info, "length"):
            duration = float(audio.info.length)
    except Exception:
        pass

    if ext == ".mp3":
        tags = _read_mp3_tags(path)
    elif ext == ".wma":
        tags = _read_wma_tags(path)
    elif ext == ".wav":
        tags = _read_wav_tags(path)
    else:
        tags = _read_generic_tags(path)

    guess_artist, guess_title = _guess_artist_title_from_filename(filename)

    return TrackFile(
        path=path,
        filename=filename,
        ext=ext,
        size_bytes=size_bytes,
        duration_seconds=duration,
        mtime_ns=mtime_ns,
        content_hash=content_hash,
        tag_artist=tags.get("artist"),
        tag_title=tags.get("title"),
        tag_album=tags.get("album"),
        tag_track_number=tags.get("tracknumber"),
        tag_year=(tags.get("date") or "")[:4] or None,
        guess_artist=guess_artist,
        guess_title=guess_title,
    )


def scan_folder(folder: str, recursive: bool = True) -> list[TrackFile]:
    if not os.path.isdir(folder):
        raise NotADirectoryError(f"Not a folder: {folder}")

    from .dedup import QUARANTINE_DIRNAME
    from . import pathsafe

    results: list[TrackFile] = []
    if recursive:
        walker = os.walk(folder)
    else:
        walker = [(folder, [], os.listdir(folder))]

    for root, dirs, files in walker:
        # Don't walk into our own quarantine folder - a file moved there
        # to get it out of the working set shouldn't reappear on the very
        # next scan of the same folder. Also drop any symlinked/reparse
        # subdirectory so a linked folder can't smuggle in external files.
        dirs[:] = pathsafe.prune_unsafe_dirs(root, [d for d in dirs if d != QUARANTINE_DIRNAME])
        for name in files:
            ext = os.path.splitext(name)[1].lower()
            if ext in SUPPORTED_EXTENSIONS:
                full_path = os.path.join(root, name)
                # Filesystem-authority guard: never admit a link/reparse
                # object or a file whose resolved path escapes the selected
                # library root (see pathsafe.py). Tagging one would follow
                # the link and mutate a file outside the user's authority.
                if not pathsafe.is_safe_scan_member(full_path, folder):
                    continue
                try:
                    results.append(read_track(full_path))
                except Exception:
                    # Skip unreadable/corrupt files rather than aborting the scan
                    continue
    results.sort(key=lambda t: t.path.lower())
    return results
