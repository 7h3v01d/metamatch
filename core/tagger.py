"""
tagger.py
Applies a confirmed MusicBrainz match back onto the local file: writing
corrected ID3/ASF tags and/or renaming the file to a clean standard
pattern: "Artist - Title.ext".
"""

from __future__ import annotations

import base64
import os
import re
import struct

from mutagen.id3 import ID3, ID3NoHeaderError, APIC
from mutagen.easyid3 import EasyID3
from mutagen.asf import ASF, ASFByteArrayAttribute
from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggvorbis import OggVorbis

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

_MIME_TO_MP4_FORMAT = {
    "image/jpeg": MP4Cover.FORMAT_JPEG,
    "image/jpg": MP4Cover.FORMAT_JPEG,
    "image/png": MP4Cover.FORMAT_PNG,
}


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


def _build_wma_picture_bytes(image_bytes: bytes, mime: str, description: str = "") -> bytes:
    """
    Builds the binary payload for an ASF WM/Picture attribute per the
    WMA cover-art convention: 1 byte type, 4 byte LE size, null-terminated
    UTF-16LE mime + description strings, then the raw image bytes.
    """
    picture_type = 3  # "front cover"
    mime_bytes = mime.encode("utf-16-le") + b"\x00\x00"
    desc_bytes = description.encode("utf-16-le") + b"\x00\x00"
    size_bytes = struct.pack("<I", len(image_bytes))
    return bytes([picture_type]) + size_bytes + mime_bytes + desc_bytes + image_bytes


def embed_cover_art(path: str, image_bytes: bytes, mime: str) -> None:
    """Embeds front-cover art into a file, dispatching by extension."""
    ext = os.path.splitext(path)[1].lower()

    if ext == ".mp3":
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=image_bytes))
        tags.save(path)

    elif ext == ".wma":
        audio = ASF(path)
        payload = _build_wma_picture_bytes(image_bytes, mime, "Cover")
        audio["WM/Picture"] = [ASFByteArrayAttribute(payload)]
        audio.save()

    elif ext == ".flac":
        audio = FLAC(path)
        audio.clear_pictures()
        pic = Picture()
        pic.data = image_bytes
        pic.type = 3
        pic.mime = mime
        audio.add_picture(pic)
        audio.save()

    elif ext == ".m4a":
        audio = MP4(path)
        fmt = _MIME_TO_MP4_FORMAT.get(mime, MP4Cover.FORMAT_JPEG)
        audio["covr"] = [MP4Cover(image_bytes, imageformat=fmt)]
        audio.save()

    elif ext == ".ogg":
        audio = OggVorbis(path)
        pic = Picture()
        pic.data = image_bytes
        pic.type = 3
        pic.mime = mime
        encoded = base64.b64encode(pic.write()).decode("ascii")
        audio["metadata_block_picture"] = [encoded]
        audio.save()

    else:
        raise ValueError(f"Cover art embedding isn't supported for {ext} files.")


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


def apply_match(
    path: str,
    match: dict,
    do_tag: bool,
    do_rename: bool,
    do_art: bool = False,
    art_bytes: bytes | None = None,
    art_mime: str | None = None,
) -> dict:
    """Applies tagging, cover art, and/or renaming for a single file. Returns a result dict."""
    result = {
        "original_path": path, "new_path": path,
        "tagged": False, "renamed": False, "art_embedded": False, "error": None,
    }
    try:
        if do_tag:
            apply_tags(path, match)
            result["tagged"] = True
        if do_art and art_bytes:
            embed_cover_art(path, art_bytes, art_mime or "image/jpeg")
            result["art_embedded"] = True
        if do_rename:
            new_path = rename_to_match(path, match)
            result["new_path"] = new_path
            result["renamed"] = new_path != path
    except Exception as e:
        result["error"] = str(e)
    return result


def set_or_clear_tags(path: str, artist=None, title=None, album=None, date=None) -> None:
    """
    Writes the given tag values, clearing any field explicitly passed as
    None. Used to restore a file's original tags on undo (as opposed to
    apply_tags, which only ever sets non-empty fields and leaves the rest
    untouched).
    """
    ext = os.path.splitext(path)[1].lower()
    fields = {"artist": artist, "title": title, "album": album, "date": date}

    if ext == ".mp3":
        try:
            tags = EasyID3(path)
        except ID3NoHeaderError:
            tags = EasyID3()
            tags.save(path)
            tags = EasyID3(path)
        for key, value in fields.items():
            if value:
                tags[key] = value
            elif key in tags:
                del tags[key]
        tags.save(path)

    elif ext == ".wma":
        audio = ASF(path)
        mapping = {"artist": "Author", "title": "Title", "album": "WM/AlbumTitle", "date": "WM/Year"}
        for key, value in fields.items():
            asf_key = mapping[key]
            if value:
                audio[asf_key] = value
            elif asf_key in audio:
                del audio[asf_key]
        audio.save()

    else:
        from mutagen import File as MutagenFile

        audio = MutagenFile(path, easy=True)
        if audio is None:
            raise ValueError(f"Unsupported or unreadable file: {path}")
        if audio.tags is None:
            audio.add_tags()
        for key, value in fields.items():
            if value:
                audio[key] = value
            elif audio.tags and key in audio.tags:
                del audio.tags[key]
        audio.save()
