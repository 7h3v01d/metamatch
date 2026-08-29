"""
movie_tagger.py
Applies a confirmed TMDB match to a local video file. Movies are handled
differently from music: most media servers (Plex, Kodi, Jellyfin) read a
`.nfo` sidecar and a poster image sitting next to the video rather than
metadata embedded inside the container, so that's the default path here.
Embedding a title/year into the container itself is offered too, but
uses different mechanisms depending on format:

  - .mp4 / .m4v: mutagen can edit these atoms directly and cheaply.
  - .mkv / .avi / .mov / .wmv: no reliable pure-Python writer, so this
    shells out to `ffmpeg -c copy` to remux with new metadata. That's a
    stream copy (no re-encoding, so it's fast and lossless) but it does
    rewrite the whole file, which matters for multi-gigabyte movies.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET

import requests

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None
MP4_DIRECT_EXTENSIONS = {".mp4", ".m4v"}
FFMPEG_REMUX_EXTENSIONS = {".mkv", ".mov", ".avi", ".wmv"}


_RESERVED_WINDOWS_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_MAX_FILENAME_LENGTH = 200


def sanitize_filename(name: str) -> str:
    cleaned = _INVALID_CHARS.sub("", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    cleaned = cleaned or "untitled"

    if len(cleaned) > _MAX_FILENAME_LENGTH:
        cleaned = cleaned[:_MAX_FILENAME_LENGTH].rstrip(" .") or "untitled"

    if cleaned.upper() in _RESERVED_WINDOWS_NAMES:
        cleaned = f"_{cleaned}"

    return cleaned


def rename_to_match(path: str, match: dict) -> str:
    folder = os.path.dirname(path)
    ext = os.path.splitext(path)[1]

    title = match.get("title") or "Unknown Title"
    year = match.get("year")
    base_name = sanitize_filename(f"{title} ({year})" if year else title)

    candidate = os.path.join(folder, base_name + ext)
    counter = 2
    while os.path.exists(candidate) and os.path.abspath(candidate) != os.path.abspath(path):
        candidate = os.path.join(folder, f"{base_name} ({counter}){ext}")
        counter += 1

    if os.path.abspath(candidate) != os.path.abspath(path):
        os.rename(path, candidate)
    return candidate


def write_nfo(path: str, match: dict) -> str:
    """Writes a Kodi/Jellyfin-compatible <movie>.nfo sidecar next to the video."""
    base = os.path.splitext(path)[0]
    nfo_path = base + ".nfo"

    root = ET.Element("movie")
    ET.SubElement(root, "title").text = match.get("title") or ""
    if match.get("original_title"):
        ET.SubElement(root, "originaltitle").text = match["original_title"]
    if match.get("year"):
        ET.SubElement(root, "year").text = str(match["year"])
    if match.get("release_date"):
        ET.SubElement(root, "premiered").text = match["release_date"]
    if match.get("overview"):
        ET.SubElement(root, "plot").text = match["overview"]
    if match.get("vote_average") is not None:
        ET.SubElement(root, "rating").text = str(match["vote_average"])
    if match.get("tmdb_id"):
        uid = ET.SubElement(root, "uniqueid", type="tmdb", default="true")
        uid.text = str(match["tmdb_id"])

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(nfo_path, encoding="utf-8", xml_declaration=True)
    return nfo_path


def download_poster(path: str, match: dict) -> str | None:
    """Saves the movie poster as '<basename>-poster.jpg' next to the video. Returns the saved path, or None."""
    poster_url = match.get("poster_url_full") or match.get("poster_url")
    if not poster_url:
        return None

    base = os.path.splitext(path)[0]
    dest = base + "-poster.jpg"
    try:
        resp = requests.get(poster_url, timeout=15)
        resp.raise_for_status()
        with open(dest, "wb") as f:
            f.write(resp.content)
        return dest
    except requests.RequestException:
        return None


def _embed_mp4_tags(path: str, match: dict) -> None:
    from mutagen.mp4 import MP4

    audio = MP4(path)
    if match.get("title"):
        audio["\xa9nam"] = [match["title"]]
    if match.get("year"):
        audio["\xa9day"] = [str(match["year"])]
    audio.save()


def _probe_stream_summary(path: str) -> list[tuple]:
    """Returns [(index, codec_type, codec_name), ...] for every stream in a file."""
    proc = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", path],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30,
    )
    try:
        data = json.loads(proc.stdout or b"{}")
    except json.JSONDecodeError:
        return []
    return [(s.get("index"), s.get("codec_type"), s.get("codec_name")) for s in data.get("streams", [])]


# Marker embedded in every remux temp file's name. On success the temp is
# os.replace()d into place and on a handled failure it's removed - but if the
# whole process is killed mid-remux, the cleanup can't run and the temp is
# orphaned. sweep_orphan_remux_temps() finds these by this marker on the next
# scan (see MovieLibrary.scan) so they don't accumulate.
REMUX_TMP_MARKER = ".metamatch_tmp"


def _remux_tmp_path(path: str) -> str:
    folder, name = os.path.split(path)
    base, ext = os.path.splitext(name)
    return os.path.join(folder, f".{base}{REMUX_TMP_MARKER}{ext}")


def sweep_orphan_remux_temps(folder: str, min_age_seconds: int = 300) -> list[str]:
    """Remove remux temp files left behind by a process that was killed
    mid-remux. Guarded by an age threshold: a temp younger than
    min_age_seconds might be an in-progress remux writing right now (its
    mtime keeps advancing as ffmpeg writes), so it's left alone. Only clearly
    stale orphans are deleted. Returns the paths removed. Never raises - a
    sweep failure must not stop a scan."""
    removed: list[str] = []
    try:
        entries = os.listdir(folder)
    except OSError:
        return removed
    now = time.time()
    for entry in entries:
        if REMUX_TMP_MARKER not in entry:
            continue
        candidate = os.path.join(folder, entry)
        try:
            if not os.path.isfile(candidate):
                continue
            if now - os.path.getmtime(candidate) < min_age_seconds:
                continue  # possibly an active remux - don't touch it
            os.remove(candidate)
            removed.append(candidate)
        except OSError:
            continue  # gone already, or permission - skip, never fatal
    return removed


def _embed_via_ffmpeg_remux(path: str, match: dict) -> None:
    if not FFMPEG_AVAILABLE:
        raise RuntimeError("ffmpeg isn't installed/available on PATH, so metadata can't be embedded for this format.")

    tmp_path = _remux_tmp_path(path)

    metadata_args = []
    if match.get("title"):
        metadata_args += ["-metadata", f"title={match['title']}"]
    if match.get("year"):
        metadata_args += ["-metadata", f"date={match['year']}"]

    # -c copy only copies whichever streams ffmpeg selects by default (one
    # video + one audio), silently dropping extra audio tracks, subtitles,
    # and attachments unless every stream is explicitly mapped. -map 0
    # keeps everything; -map_metadata/-map_chapters 0 keep the rest of the
    # container's metadata and chapter list too.
    cmd = [
        "ffmpeg", "-y", "-i", path,
        "-map", "0", "-map_metadata", "0", "-map_chapters", "0",
        "-c", "copy", *metadata_args, tmp_path,
    ]
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=1800)
    if proc.returncode != 0 or not os.path.exists(tmp_path):
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise RuntimeError(f"ffmpeg remux failed: {proc.stderr.decode(errors='replace')[-400:]}")

    # Fail closed: verify the remux kept every stream before replacing the
    # original. A stream-count/type mismatch means something was dropped
    # (a codec ffmpeg couldn't -c copy, an unusual container quirk, etc.)
    # - in that case, discard the temp file and leave the original intact
    # rather than silently losing audio/subtitle tracks.
    original_streams = _probe_stream_summary(path)
    remuxed_streams = _probe_stream_summary(tmp_path)
    original_types = sorted((t, c) for _, t, c in original_streams)
    remuxed_types = sorted((t, c) for _, t, c in remuxed_streams)

    if not remuxed_streams or original_types != remuxed_types:
        os.remove(tmp_path)
        raise RuntimeError(
            f"Refused to embed metadata: the remux would have changed the file's streams "
            f"(had {original_types}, remux produced {remuxed_types}). "
            "The original file was left untouched."
        )

    os.replace(tmp_path, path)


def embed_metadata(path: str, match: dict) -> None:
    ext = os.path.splitext(path)[1].lower()
    if ext in MP4_DIRECT_EXTENSIONS:
        _embed_mp4_tags(path, match)
    elif ext in FFMPEG_REMUX_EXTENSIONS:
        _embed_via_ffmpeg_remux(path, match)
    else:
        raise ValueError(f"Embedding metadata isn't supported for {ext} files.")


def _safe_move(old_path: str, new_path: str) -> str:
    """Moves old_path to new_path, appending a numeric suffix if new_path is
    already taken by an unrelated file, rather than silently overwriting it."""
    folder = os.path.dirname(new_path)
    base, ext = os.path.splitext(os.path.basename(new_path))
    candidate = new_path
    counter = 2
    while os.path.exists(candidate) and os.path.abspath(candidate) != os.path.abspath(old_path):
        candidate = os.path.join(folder, f"{base} ({counter}){ext}")
        counter += 1
    os.rename(old_path, candidate)
    return candidate


def apply_movie_match(
    path: str,
    match: dict,
    do_tag: bool = False,
    do_rename: bool = False,
    do_nfo: bool = False,
    do_poster: bool = False,
) -> dict:
    result = {
        "original_path": path, "new_path": path,
        "tagged": False, "renamed": False, "nfo_path": None, "poster_path": None,
        "error": None,
    }
    try:
        current_path = path
        if do_tag:
            embed_metadata(current_path, match)
            result["tagged"] = True
        if do_nfo:
            result["nfo_path"] = write_nfo(current_path, match)
        if do_poster:
            result["poster_path"] = download_poster(current_path, match)
        if do_rename:
            new_path = rename_to_match(current_path, match)
            # Sidecars were written against the old name - rename them to
            # match, without clobbering an unrelated file that happens to
            # already sit at that path (_safe_move appends "(2)" etc.
            # instead, same as the video rename above does).
            if new_path != current_path:
                for key, suffix in (("nfo_path", ".nfo"), ("poster_path", "-poster.jpg")):
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
