"""
tv_tagger.py
Applies a confirmed TMDB episode match back onto a local episode file:
embedding TV metadata atoms (or an ffmpeg remux for mkv/avi/mov/wmv),
writing a Kodi/Jellyfin <episodedetails> .nfo sidecar, saving the episode
still as a thumbnail, and renaming to the Plex/Kodi standard
"Show Name - S01E02 - Episode Title.ext".

It reuses movie_tagger for everything that isn't TV-specific - filename
sanitising, the collision-safe sidecar move, and above all the fail-closed
ffmpeg remux (temp file -> stream-preservation check -> os.replace, with
killed-ffmpeg and orphan-temp handling) - so the two video paths share one
audited implementation of the dangerous part.

Mutation order matches the movie tagger exactly (embed -> nfo -> thumb ->
rename, rename last), which is what lets TvLibrary's rollback compensate a
failed apply purely in place without ever having to move a file back.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET

import requests

from .movie_tagger import (
    sanitize_filename, _safe_move, remux_with_metadata,
    MP4_DIRECT_EXTENSIONS, FFMPEG_REMUX_EXTENSIONS, sidecar_is_protected,
)

THUMB_SUFFIX = "-thumb.jpg"


def _season_episode_tag(match: dict) -> str:
    """'S01E02', or 'S01E02-E03' for a multi-episode file."""
    season = match.get("season") or 0
    episodes = match.get("episodes") or [match.get("episode")]
    episodes = [e for e in episodes if e is not None] or [0]
    head = f"S{int(season):02d}E{int(episodes[0]):02d}"
    if len(episodes) > 1:
        head += f"-E{int(episodes[-1]):02d}"
    return head


def rename_to_match(path: str, match: dict) -> str:
    folder = os.path.dirname(path)
    ext = os.path.splitext(path)[1]

    show = match.get("series_name") or "Unknown Series"
    tag = _season_episode_tag(match)
    title = match.get("episode_title")
    base = f"{show} - {tag} - {title}" if title else f"{show} - {tag}"
    base_name = sanitize_filename(base)

    candidate = os.path.join(folder, base_name + ext)
    counter = 2
    while os.path.exists(candidate) and os.path.abspath(candidate) != os.path.abspath(path):
        candidate = os.path.join(folder, f"{base_name} ({counter}){ext}")
        counter += 1

    if os.path.abspath(candidate) != os.path.abspath(path):
        os.rename(path, candidate)
    return candidate


def write_nfo(path: str, match: dict) -> str:
    """Writes a Kodi/Jellyfin-compatible <episodedetails>.nfo next to the file."""
    base = os.path.splitext(path)[0]
    nfo_path = base + ".nfo"

    root = ET.Element("episodedetails")
    ET.SubElement(root, "title").text = match.get("episode_title") or ""
    ET.SubElement(root, "showtitle").text = match.get("series_name") or ""
    if match.get("season") is not None:
        ET.SubElement(root, "season").text = str(match["season"])
    if match.get("episode") is not None:
        ET.SubElement(root, "episode").text = str(match["episode"])
    if match.get("air_date"):
        ET.SubElement(root, "aired").text = match["air_date"]
    if match.get("episode_overview"):
        ET.SubElement(root, "plot").text = match["episode_overview"]
    if match.get("vote_average") is not None:
        ET.SubElement(root, "rating").text = str(match["vote_average"])
    if match.get("series_tmdb_id"):
        uid = ET.SubElement(root, "uniqueid", type="tmdb", default="true")
        uid.text = str(match["series_tmdb_id"])

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(nfo_path, encoding="utf-8", xml_declaration=True)
    return nfo_path


def download_thumb(path: str, match: dict) -> str | None:
    """Saves the episode still as '<basename>-thumb.jpg' (Kodi episode
    thumbnail convention). Returns the saved path, or None."""
    url = match.get("still_url_full") or match.get("still_url")
    if not url:
        return None
    dest = os.path.splitext(path)[0] + THUMB_SUFFIX
    if sidecar_is_protected(dest):
        return None  # don't overwrite a pre-existing thumb we couldn't restore
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        with open(dest, "wb") as f:
            f.write(resp.content)
        return dest
    except requests.RequestException:
        return None


def _embed_mp4_tags(path: str, match: dict) -> None:
    from mutagen.mp4 import MP4

    audio = MP4(path)
    if match.get("episode_title"):
        audio["\xa9nam"] = [match["episode_title"]]
    if match.get("series_name"):
        audio["tvsh"] = [match["series_name"]]
        audio["\xa9ART"] = [match["series_name"]]
    if match.get("season") is not None:
        audio["tvsn"] = [int(match["season"])]
    if match.get("episode") is not None:
        audio["tves"] = [int(match["episode"])]
    if match.get("air_date"):
        audio["\xa9day"] = [str(match["air_date"])[:4]]
    # stik=10 marks the file as a TV Show in players that read iTunes atoms.
    audio["stik"] = [10]
    audio.save()


def embed_metadata(path: str, match: dict) -> None:
    ext = os.path.splitext(path)[1].lower()
    if ext in MP4_DIRECT_EXTENSIONS:
        _embed_mp4_tags(path, match)
    elif ext in FFMPEG_REMUX_EXTENSIONS:
        # Container-level tags ffmpeg understands across mkv/avi/mov/wmv.
        remux_with_metadata(path, {
            "title": match.get("episode_title"),
            "show": match.get("series_name"),
            "season_number": match.get("season"),
            "episode_sort": match.get("episode"),
            "episode_id": match.get("episode"),
            "date": str(match["air_date"])[:4] if match.get("air_date") else None,
        })
    else:
        raise ValueError(f"Embedding metadata isn't supported for {ext} files.")


SERIES_NFO_NAME = "tvshow.nfo"
SERIES_POSTER_NAME = "poster.jpg"


def series_nfo_path(series_root: str) -> str:
    return os.path.join(series_root, SERIES_NFO_NAME)


def series_poster_path(series_root: str) -> str:
    return os.path.join(series_root, SERIES_POSTER_NAME)


def season_poster_path(series_root: str, season: int) -> str:
    # Kodi convention: seasonXX-poster.jpg at the show root.
    return os.path.join(series_root, f"season{int(season):02d}-poster.jpg")


def write_tvshow_nfo(series_root: str, details: dict) -> str:
    """Writes a Kodi/Jellyfin <tvshow>.nfo at the series root."""
    nfo_path = series_nfo_path(series_root)
    root = ET.Element("tvshow")
    ET.SubElement(root, "title").text = details.get("name") or ""
    if details.get("overview"):
        ET.SubElement(root, "plot").text = details["overview"]
    if details.get("first_air_date"):
        ET.SubElement(root, "premiered").text = details["first_air_date"]
    if details.get("year"):
        ET.SubElement(root, "year").text = str(details["year"])
    if details.get("vote_average") is not None:
        ET.SubElement(root, "rating").text = str(details["vote_average"])
    if details.get("status"):
        ET.SubElement(root, "status").text = details["status"]
    for genre in details.get("genres") or []:
        ET.SubElement(root, "genre").text = genre
    if details.get("series_tmdb_id"):
        uid = ET.SubElement(root, "uniqueid", type="tmdb", default="true")
        uid.text = str(details["series_tmdb_id"])

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(nfo_path, encoding="utf-8", xml_declaration=True)
    return nfo_path


def download_image(url: str, dest: str) -> str | None:
    """Best-effort image download to `dest`; returns the path or None. Refuses
    to overwrite a pre-existing image too large to back up for undo."""
    if not url:
        return None
    if sidecar_is_protected(dest):
        return None
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        with open(dest, "wb") as f:
            f.write(resp.content)
        return dest
    except requests.RequestException:
        return None


def apply_episode_match(
    path: str,
    match: dict,
    do_tag: bool = False,
    do_rename: bool = True,
    do_nfo: bool = True,
    do_thumb: bool = True,
) -> dict:
    """Applies embedding, .nfo, thumbnail, and/or rename for one episode.
    Mirrors movie_tagger.apply_movie_match's contract and mutation order
    (embed -> nfo -> thumb -> rename), returning a result dict with the same
    error-capture shape."""
    result = {
        "original_path": path, "new_path": path,
        "tagged": False, "renamed": False, "nfo_path": None, "thumb_path": None,
        "error": None,
    }
    try:
        current_path = path
        if do_tag:
            embed_metadata(current_path, match)
            result["tagged"] = True
        if do_nfo:
            result["nfo_path"] = write_nfo(current_path, match)
        if do_thumb:
            thumb_dest = os.path.splitext(current_path)[0] + THUMB_SUFFIX
            if sidecar_is_protected(thumb_dest):
                result.setdefault("warnings", []).append(
                    "Left the existing thumbnail in place: it's larger than MetaMatch can "
                    "back up for undo, so it wasn't overwritten.")
            else:
                result["thumb_path"] = download_thumb(current_path, match)
        if do_rename:
            new_path = rename_to_match(current_path, match)
            if new_path != current_path:
                for key, suffix in (("nfo_path", ".nfo"), ("thumb_path", THUMB_SUFFIX)):
                    old_sidecar = result.get(key)
                    if old_sidecar and os.path.exists(old_sidecar):
                        new_sidecar = os.path.splitext(new_path)[0] + suffix
                        try:
                            result[key] = _safe_move(old_sidecar, new_sidecar)
                        except OSError as e:
                            result.setdefault("warnings", []).append(
                                f"Could not rename {key.replace('_path', '')} sidecar: {e}"
                            )
            result["new_path"] = new_path
            result["renamed"] = new_path != current_path
    except Exception as e:
        result["error"] = str(e)
    return result
