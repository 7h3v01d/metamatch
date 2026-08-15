"""
tagger.py
Applies a confirmed MusicBrainz match back onto the local file: writing
corrected ID3/ASF tags and/or renaming the file to a clean standard
pattern: "Artist - Title.ext".
"""

from __future__ import annotations

import os
import re

from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TALB, TDRC
from mutagen.easyid3 import EasyID3
from mutagen.asf import ASF

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(name: str) -> str:
    cleaned = _INVALID_CHARS.sub("", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or "untitled"


def apply_tags(path: str, match: dict) -> None:
    ext = os.path.splitext(path)[1].lower()
    artist = match.get("artist") or ""
    title = match.get("title") or ""
    album = match.get("album") or ""
    date = (match.get("date") or "")[:4]

    if ext == ".mp3":
        try:
            tags = EasyID3(path)
        except ID3NoHeaderError:
            tags = EasyID3()
            tags.save(path)
            tags = EasyID3(path)
        if artist:
            tags["artist"] = artist
        if title:
            tags["title"] = title
        if album:
            tags["album"] = album
        if date:
            tags["date"] = date
        tags.save(path)

    elif ext == ".wma":
        audio = ASF(path)
        if artist:
            audio["Author"] = artist
        if title:
            audio["Title"] = title
        if album:
            audio["WM/AlbumTitle"] = album
        if date:
            audio["WM/Year"] = date
        audio.save()

    else:
        # flac/m4a/ogg via mutagen's easy interface
        from mutagen import File as MutagenFile

        audio = MutagenFile(path, easy=True)
        if audio is None:
            raise ValueError(f"Unsupported or unreadable file for tagging: {path}")
        if audio.tags is None:
            audio.add_tags()
        if artist:
            audio["artist"] = artist
        if title:
            audio["title"] = title
        if album:
            audio["album"] = album
        if date:
            audio["date"] = date
        audio.save()


def rename_to_match(path: str, match: dict) -> str:
    """Renames the file to 'Artist - Title.ext', avoiding collisions. Returns the new path."""
    folder = os.path.dirname(path)
    ext = os.path.splitext(path)[1]

    artist = match.get("artist") or "Unknown Artist"
    title = match.get("title") or "Unknown Title"
    base_name = sanitize_filename(f"{artist} - {title}")

    candidate = os.path.join(folder, base_name + ext)
    counter = 2
    while os.path.exists(candidate) and os.path.abspath(candidate) != os.path.abspath(path):
        candidate = os.path.join(folder, f"{base_name} ({counter}){ext}")
        counter += 1

    if os.path.abspath(candidate) != os.path.abspath(path):
        os.rename(path, candidate)
    return candidate


def apply_match(path: str, match: dict, do_tag: bool, do_rename: bool) -> dict:
    """Applies tagging and/or renaming for a single file. Returns a result dict."""
    result = {"original_path": path, "new_path": path, "tagged": False, "renamed": False, "error": None}
    try:
        if do_tag:
            apply_tags(path, match)
            result["tagged"] = True
        if do_rename:
            new_path = rename_to_match(path, match)
            result["new_path"] = new_path
            result["renamed"] = new_path != path
    except Exception as e:
        result["error"] = str(e)
    return result
