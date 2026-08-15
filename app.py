"""
app.py
Local web UI for MetaMatch. Run with `python app.py`, then open
http://127.0.0.1:5050 in a browser.

Music flow:
  1. POST /api/scan               - scan a folder for audio files
  2. POST /api/match/start        - kick off background MusicBrainz matching
  3. GET  /api/match/progress     - poll matching progress
  4. GET  /api/tracks             - fetch current track + match data
  5. GET  /api/art/<release_id>   - proxy a cover-art thumbnail for the UI
  6. POST /api/apply              - tag/rename/art a single file
  7. POST /api/apply_all          - tag/rename/art every file above a confidence bar
  8. POST /api/undo               - revert a previously-applied file
  9. POST /api/undo_all           - revert every applied file still reversible
 10. POST /api/duplicates/scan    - find exact + probable duplicate groups
 11. POST /api/duplicates/quarantine - move selected files into a duplicates folder
 12. GET  /api/export_csv         - download a CSV report of matches

Movie flow (needs a TMDB API key - see /api/settings/tmdb):
  1. GET/POST /api/settings/tmdb  - check/save the TMDB API key
  2. POST /api/movies/scan        - scan a folder for video files
  3. POST /api/movies/match/start - kick off background TMDB matching
  4. GET  /api/movies/match/progress
  5. GET  /api/movies             - fetch current video + match data
  6. POST /api/movies/apply       - rename/tag/write nfo+poster for one file
  7. POST /api/movies/apply_all   - same, in bulk above a confidence bar
  8. POST /api/movies/undo        - revert a previously-applied file
  9. POST /api/movies/undo_all    - revert every applied file still reversible
 10. POST /api/movies/duplicates/scan       - find exact + probable duplicate groups
 11. POST /api/movies/duplicates/quarantine - move selected files (+ sidecars) aside
 12. GET  /api/movies/export_csv
"""

from __future__ import annotations

import csv
import io
import os
import threading

from flask import Flask, jsonify, request, render_template, send_file, Response

from core.scanner import scan_folder, read_track, TrackFile
from core.matcher import match_tracks
from core.tagger import apply_match, set_or_clear_tags
from core.art import fetch_cover_art
from core import dedup

from core.video_scanner import scan_folder as scan_video_folder, read_video, VideoFile, FFPROBE_AVAILABLE
from core.movie_matcher import match_videos, TmdbNotConfigured
from core.movie_tagger import apply_movie_match, FFMPEG_AVAILABLE
from core import config as app_config

app = Flask(__name__)

# ---------------------------------------------------------------------------
# In-memory session state. This is a single-user local tool, so one global
# state dict (guarded by a lock for the background matching thread) is
# simpler and more transparent than standing up a database.
# ---------------------------------------------------------------------------
STATE = {
    "folder": None,
    "tracks": {},          # path -> TrackFile
    "order": [],           # list of paths, preserves scan order
    "match_progress": {"running": False, "done": 0, "total": 0},
    "undo_by_path": {},    # current path -> undo record dict
}
STATE_LOCK = threading.Lock()

MOVIE_STATE = {
    "folder": None,
    "videos": {},           # path -> VideoFile
    "order": [],
    "match_progress": {"running": False, "done": 0, "total": 0, "error": None},
    "undo_by_path": {},     # current path -> undo record dict
}
MOVIE_STATE_LOCK = threading.Lock()


def _movies_payload() -> list[dict]:
    with MOVIE_STATE_LOCK:
        out = []
        for p in MOVIE_STATE["order"]:
            d = MOVIE_STATE["videos"][p].to_dict()
            d["can_undo"] = p in MOVIE_STATE["undo_by_path"]
            out.append(d)
        return out


def _tracks_payload() -> list[dict]:
    with STATE_LOCK:
        out = []
        for p in STATE["order"]:
            d = STATE["tracks"][p].to_dict()
            d["can_undo"] = p in STATE["undo_by_path"]
            out.append(d)
        return out


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/scan", methods=["POST"])
def api_scan():
    data = request.get_json(force=True) or {}
    folder = (data.get("folder") or "").strip()
    recursive = bool(data.get("recursive", True))

    if not folder:
        return jsonify({"error": "Please provide a folder path."}), 400
    if not os.path.isdir(folder):
        return jsonify({"error": f"'{folder}' is not a folder that exists on this machine."}), 400

    try:
        tracks = scan_folder(folder, recursive=recursive)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    with STATE_LOCK:
        STATE["folder"] = folder
        STATE["tracks"] = {t.path: t for t in tracks}
        STATE["order"] = [t.path for t in tracks]
        STATE["match_progress"] = {"running": False, "done": 0, "total": len(tracks)}
        STATE["undo_by_path"] = {}

    return jsonify({"folder": folder, "count": len(tracks), "tracks": _tracks_payload()})


def _run_matching():
    with STATE_LOCK:
        tracks = [STATE["tracks"][p] for p in STATE["order"]]
        STATE["match_progress"] = {"running": True, "done": 0, "total": len(tracks)}

    def on_progress(done, total):
        with STATE_LOCK:
            STATE["match_progress"]["done"] = done
            STATE["match_progress"]["total"] = total

    match_tracks(tracks, progress_callback=on_progress)

    with STATE_LOCK:
        STATE["match_progress"]["running"] = False


@app.route("/api/match/start", methods=["POST"])
def api_match_start():
    with STATE_LOCK:
        if not STATE["order"]:
            return jsonify({"error": "Scan a folder first."}), 400
        if STATE["match_progress"]["running"]:
            return jsonify({"error": "Matching is already running."}), 409

    thread = threading.Thread(target=_run_matching, daemon=True)
    thread.start()
    return jsonify({"started": True})


@app.route("/api/match/progress")
def api_match_progress():
    with STATE_LOCK:
        return jsonify(STATE["match_progress"])


@app.route("/api/tracks")
def api_tracks():
    return jsonify({"folder": STATE["folder"], "tracks": _tracks_payload()})


@app.route("/api/art/<path:release_id>")
def api_art(release_id):
    """Proxies a small cover-art thumbnail so the browser can show it without
    the Cover Art Archive needing CORS headers for our origin."""
    result = fetch_cover_art(release_id, size="250")
    if not result:
        return "", 404
    image_bytes, mime = result
    return Response(image_bytes, mimetype=mime)


def _snapshot_original_tags(track: TrackFile) -> dict:
    return {
        "artist": track.tag_artist,
        "title": track.tag_title,
        "album": track.tag_album,
        "date": track.tag_year,
    }


def _record_undo(original_path: str, new_path: str, original_tags: dict) -> None:
    record = {"original_path": original_path, "new_path": new_path, "original_tags": original_tags}
    with STATE_LOCK:
        # A file can only be undone back to the state it was in the last
        # time we saw it - re-applying overwrites any earlier undo record.
        STATE["undo_by_path"].pop(original_path, None)
        STATE["undo_by_path"][new_path] = record


def _apply_one(track: TrackFile, do_tag: bool, do_rename: bool, do_art: bool) -> dict:
    original_tags = _snapshot_original_tags(track)

    art_bytes = art_mime = None
    if do_art and track.match and track.match.get("release_id"):
        fetched = fetch_cover_art(track.match["release_id"], size="500")
        if fetched:
            art_bytes, art_mime = fetched

    result = apply_match(
        track.path, track.match, do_tag=do_tag, do_rename=do_rename,
        do_art=do_art, art_bytes=art_bytes, art_mime=art_mime,
    )

    if not result["error"]:
        _record_undo(track.path, result["new_path"], original_tags)

        if result["new_path"] != track.path:
            with STATE_LOCK:
                if track.path in STATE["tracks"]:
                    del STATE["tracks"][track.path]
                new_track = read_track(result["new_path"])
                new_track.match = track.match
                STATE["tracks"][new_track.path] = new_track
                if track.path in STATE["order"]:
                    idx = STATE["order"].index(track.path)
                    STATE["order"][idx] = new_track.path
        else:
            with STATE_LOCK:
                if track.path in STATE["tracks"]:
                    refreshed = read_track(track.path)
                    refreshed.match = track.match
                    STATE["tracks"][track.path] = refreshed

    return result


@app.route("/api/apply", methods=["POST"])
def api_apply():
    data = request.get_json(force=True) or {}
    track_id = data.get("id")
    do_tag = bool(data.get("tag", True))
    do_rename = bool(data.get("rename", True))
    do_art = bool(data.get("art", False))

    with STATE_LOCK:
        track = STATE["tracks"].get(track_id)

    if not track:
        return jsonify({"error": "Unknown track."}), 404
    if not track.match:
        return jsonify({"error": "This track has no match to apply."}), 400

    result = _apply_one(track, do_tag, do_rename, do_art)
    return jsonify(result)


@app.route("/api/apply_all", methods=["POST"])
def api_apply_all():
    data = request.get_json(force=True) or {}
    do_tag = bool(data.get("tag", True))
    do_rename = bool(data.get("rename", True))
    do_art = bool(data.get("art", False))
    min_confidence = float(data.get("min_confidence", 75))

    with STATE_LOCK:
        candidates = [STATE["tracks"][p] for p in STATE["order"]]

    results = []
    for track in candidates:
        if not track.match or track.match.get("confidence", 0) < min_confidence:
            continue
        results.append(_apply_one(track, do_tag, do_rename, do_art))

    return jsonify({"applied": len(results), "results": results})


@app.route("/api/undo", methods=["POST"])
def api_undo():
    data = request.get_json(force=True) or {}
    track_id = data.get("id")

    with STATE_LOCK:
        record = STATE["undo_by_path"].get(track_id)

    if not record:
        return jsonify({"error": "Nothing to undo for this file."}), 400

    result = _undo_one(record)
    if result["error"]:
        return jsonify(result), 500
    return jsonify(result)


def _undo_one(record: dict) -> dict:
    current_path = record["new_path"]
    original_path = record["original_path"]
    result = {"restored_path": current_path, "error": None}
    try:
        path_to_tag = current_path
        if current_path != original_path and os.path.exists(current_path):
            if os.path.exists(original_path):
                raise FileExistsError(
                    f"Can't restore the original filename - '{os.path.basename(original_path)}' "
                    "already exists again in that folder."
                )
            os.rename(current_path, original_path)
            path_to_tag = original_path

        set_or_clear_tags(path_to_tag, **record["original_tags"])
        result["restored_path"] = path_to_tag

        with STATE_LOCK:
            if current_path in STATE["tracks"]:
                del STATE["tracks"][current_path]
            refreshed = read_track(path_to_tag)
            STATE["tracks"][refreshed.path] = refreshed
            if current_path in STATE["order"]:
                idx = STATE["order"].index(current_path)
                STATE["order"][idx] = refreshed.path
            STATE["undo_by_path"].pop(current_path, None)

    except Exception as e:
        result["error"] = str(e)
    return result


@app.route("/api/undo_all", methods=["POST"])
def api_undo_all():
    with STATE_LOCK:
        records = list(STATE["undo_by_path"].values())

    results = [_undo_one(r) for r in records]
    succeeded = sum(1 for r in results if not r["error"])
    return jsonify({"restored": succeeded, "results": results})


@app.route("/api/duplicates/scan", methods=["POST"])
def api_duplicates_scan():
    with STATE_LOCK:
        tracks = [STATE["tracks"][p] for p in STATE["order"]]

    if not tracks:
        return jsonify({"error": "Scan a folder first."}), 400

    exact = dedup.find_exact_duplicates(tracks)
    probable = dedup.find_probable_duplicates(tracks)
    return jsonify({"exact": exact, "probable": probable})


@app.route("/api/duplicates/quarantine", methods=["POST"])
def api_duplicates_quarantine():
    data = request.get_json(force=True) or {}
    paths = data.get("paths") or []

    with STATE_LOCK:
        folder = STATE["folder"]

    if not folder:
        return jsonify({"error": "Scan a folder first."}), 400
    if not paths:
        return jsonify({"error": "No files selected."}), 400

    results = dedup.quarantine(paths, folder)

    with STATE_LOCK:
        for r in results:
            if not r["error"] and r["original_path"] in STATE["tracks"]:
                del STATE["tracks"][r["original_path"]]
                if r["original_path"] in STATE["order"]:
                    STATE["order"].remove(r["original_path"])
                STATE["undo_by_path"].pop(r["original_path"], None)

    moved = sum(1 for r in results if not r["error"])
    return jsonify({"moved": moved, "results": results})


@app.route("/api/export_csv")
def api_export_csv():
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "file_path", "current_artist", "current_title", "current_album",
        "matched_artist", "matched_title", "matched_album", "matched_date",
        "confidence", "musicbrainz_url",
    ])
    with STATE_LOCK:
        for p in STATE["order"]:
            t = STATE["tracks"][p]
            m = t.match or {}
            writer.writerow([
                t.path, t.tag_artist or "", t.tag_title or "", t.tag_album or "",
                m.get("artist", ""), m.get("title", ""), m.get("album", ""), m.get("date", ""),
                m.get("confidence", ""), m.get("musicbrainz_url", ""),
            ])
    mem = io.BytesIO(buf.getvalue().encode("utf-8"))
    mem.seek(0)
    return send_file(mem, mimetype="text/csv", as_attachment=True, download_name="metamatch_report.csv")


# ---------------------------------------------------------------------------
# Movies (TMDB)
# ---------------------------------------------------------------------------

@app.route("/api/settings/tmdb", methods=["GET"])
def api_settings_tmdb_get():
    key = app_config.get_tmdb_api_key()
    return jsonify({
        "configured": bool(key),
        "masked_key": app_config.mask_key(key) if key else None,
        "ffmpeg_available": FFMPEG_AVAILABLE,
        "ffprobe_available": FFPROBE_AVAILABLE,
    })


@app.route("/api/settings/tmdb", methods=["POST"])
def api_settings_tmdb_set():
    data = request.get_json(force=True) or {}
    key = (data.get("api_key") or "").strip()
    if not key:
        return jsonify({"error": "Enter an API key."}), 400
    app_config.set_tmdb_api_key(key)
    return jsonify({"configured": True, "masked_key": app_config.mask_key(key)})


@app.route("/api/movies/scan", methods=["POST"])
def api_movies_scan():
    data = request.get_json(force=True) or {}
    folder = (data.get("folder") or "").strip()
    recursive = bool(data.get("recursive", True))

    if not folder:
        return jsonify({"error": "Please provide a folder path."}), 400
    if not os.path.isdir(folder):
        return jsonify({"error": f"'{folder}' is not a folder that exists on this machine."}), 400

    try:
        videos = scan_video_folder(folder, recursive=recursive)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    with MOVIE_STATE_LOCK:
        MOVIE_STATE["folder"] = folder
        MOVIE_STATE["videos"] = {v.path: v for v in videos}
        MOVIE_STATE["order"] = [v.path for v in videos]
        MOVIE_STATE["match_progress"] = {"running": False, "done": 0, "total": len(videos), "error": None}
        MOVIE_STATE["undo_by_path"] = {}

    return jsonify({
        "folder": folder, "count": len(videos), "videos": _movies_payload(),
        "ffprobe_available": FFPROBE_AVAILABLE,
    })


def _run_movie_matching():
    with MOVIE_STATE_LOCK:
        videos = [MOVIE_STATE["videos"][p] for p in MOVIE_STATE["order"]]
        MOVIE_STATE["match_progress"] = {"running": True, "done": 0, "total": len(videos), "error": None}

    def on_progress(done, total):
        with MOVIE_STATE_LOCK:
            MOVIE_STATE["match_progress"]["done"] = done
            MOVIE_STATE["match_progress"]["total"] = total

    error_message = None
    try:
        match_videos(videos, progress_callback=on_progress)
    except TmdbNotConfigured as e:
        error_message = str(e)

    with MOVIE_STATE_LOCK:
        MOVIE_STATE["match_progress"]["running"] = False
        MOVIE_STATE["match_progress"]["error"] = error_message


@app.route("/api/movies/match/start", methods=["POST"])
def api_movies_match_start():
    if not app_config.get_tmdb_api_key():
        return jsonify({"error": "Add a TMDB API key in settings first."}), 400
    with MOVIE_STATE_LOCK:
        if not MOVIE_STATE["order"]:
            return jsonify({"error": "Scan a folder first."}), 400
        if MOVIE_STATE["match_progress"]["running"]:
            return jsonify({"error": "Matching is already running."}), 409

    thread = threading.Thread(target=_run_movie_matching, daemon=True)
    thread.start()
    return jsonify({"started": True})


@app.route("/api/movies/match/progress")
def api_movies_match_progress():
    with MOVIE_STATE_LOCK:
        return jsonify(MOVIE_STATE["match_progress"])


@app.route("/api/movies")
def api_movies_list():
    return jsonify({"folder": MOVIE_STATE["folder"], "videos": _movies_payload()})


def _snapshot_movie_original(video: VideoFile) -> dict:
    base = os.path.splitext(video.path)[0]
    return {
        "tag_title": video.tag_title,
        "tag_year": video.tag_year,
        "had_nfo": os.path.exists(base + ".nfo"),
        "had_poster": os.path.exists(base + "-poster.jpg"),
    }


def _record_movie_undo(original_path: str, new_path: str, snapshot: dict) -> None:
    record = {"original_path": original_path, "new_path": new_path, **snapshot}
    with MOVIE_STATE_LOCK:
        MOVIE_STATE["undo_by_path"].pop(original_path, None)
        MOVIE_STATE["undo_by_path"][new_path] = record


def _apply_one_movie(video: VideoFile, do_tag: bool, do_rename: bool, do_nfo: bool, do_poster: bool) -> dict:
    snapshot = _snapshot_movie_original(video)

    result = apply_movie_match(
        video.path, video.match, do_tag=do_tag, do_rename=do_rename, do_nfo=do_nfo, do_poster=do_poster,
    )

    if not result["error"]:
        _record_movie_undo(video.path, result["new_path"], snapshot)

        if result["new_path"] != video.path:
            with MOVIE_STATE_LOCK:
                if video.path in MOVIE_STATE["videos"]:
                    del MOVIE_STATE["videos"][video.path]
                new_video = read_video(result["new_path"])
                new_video.match = video.match
                MOVIE_STATE["videos"][new_video.path] = new_video
                if video.path in MOVIE_STATE["order"]:
                    idx = MOVIE_STATE["order"].index(video.path)
                    MOVIE_STATE["order"][idx] = new_video.path
        else:
            with MOVIE_STATE_LOCK:
                if video.path in MOVIE_STATE["videos"]:
                    refreshed = read_video(video.path)
                    refreshed.match = video.match
                    MOVIE_STATE["videos"][video.path] = refreshed
    return result


@app.route("/api/movies/apply", methods=["POST"])
def api_movies_apply():
    data = request.get_json(force=True) or {}
    video_id = data.get("id")
    do_tag = bool(data.get("tag", False))
    do_rename = bool(data.get("rename", True))
    do_nfo = bool(data.get("nfo", True))
    do_poster = bool(data.get("poster", True))

    with MOVIE_STATE_LOCK:
        video = MOVIE_STATE["videos"].get(video_id)

    if not video:
        return jsonify({"error": "Unknown file."}), 404
    if not video.match:
        return jsonify({"error": "This file has no match to apply."}), 400

    result = _apply_one_movie(video, do_tag, do_rename, do_nfo, do_poster)
    return jsonify(result)


@app.route("/api/movies/apply_all", methods=["POST"])
def api_movies_apply_all():
    data = request.get_json(force=True) or {}
    do_tag = bool(data.get("tag", False))
    do_rename = bool(data.get("rename", True))
    do_nfo = bool(data.get("nfo", True))
    do_poster = bool(data.get("poster", True))
    min_confidence = float(data.get("min_confidence", 75))

    with MOVIE_STATE_LOCK:
        candidates = [MOVIE_STATE["videos"][p] for p in MOVIE_STATE["order"]]

    results = []
    for video in candidates:
        if not video.match or video.match.get("confidence", 0) < min_confidence:
            continue
        results.append(_apply_one_movie(video, do_tag, do_rename, do_nfo, do_poster))

    return jsonify({"applied": len(results), "results": results})


@app.route("/api/movies/undo", methods=["POST"])
def api_movies_undo():
    data = request.get_json(force=True) or {}
    video_id = data.get("id")

    with MOVIE_STATE_LOCK:
        record = MOVIE_STATE["undo_by_path"].get(video_id)

    if not record:
        return jsonify({"error": "Nothing to undo for this file."}), 400

    result = _undo_one_movie(record)
    if result["error"]:
        return jsonify(result), 500
    return jsonify(result)


def _undo_one_movie(record: dict) -> dict:
    current_path = record["new_path"]
    original_path = record["original_path"]
    result = {"restored_path": current_path, "error": None}
    try:
        base_current = os.path.splitext(current_path)[0]
        nfo_current = base_current + ".nfo"
        poster_current = base_current + "-poster.jpg"

        path_to_use = current_path
        if current_path != original_path and os.path.exists(current_path):
            if os.path.exists(original_path):
                raise FileExistsError(
                    f"Can't restore the original filename - '{os.path.basename(original_path)}' "
                    "already exists again in that folder."
                )
            os.rename(current_path, original_path)
            path_to_use = original_path

            base_original = os.path.splitext(original_path)[0]
            if os.path.exists(nfo_current):
                new_nfo = base_original + ".nfo"
                os.rename(nfo_current, new_nfo)
                nfo_current = new_nfo
            if os.path.exists(poster_current):
                new_poster = base_original + "-poster.jpg"
                os.rename(poster_current, new_poster)
                poster_current = new_poster

        # Sidecars we created fresh (didn't exist before this apply) get
        # removed on undo. Ones that already existed are left alone - we
        # can't safely restore their original content, only their path.
        if not record.get("had_nfo") and os.path.exists(nfo_current):
            os.remove(nfo_current)
        if not record.get("had_poster") and os.path.exists(poster_current):
            os.remove(poster_current)

        # Embedded-tag revert is only reliable for mp4/m4v (direct atom
        # edit, cheap to undo). mkv/avi/mov/wmv go through an ffmpeg remux
        # to embed, which isn't cheaply reversible, so those are left as-is
        # - consistent with how music album art embedding is handled.
        ext = os.path.splitext(path_to_use)[1].lower()
        if ext in (".mp4", ".m4v"):
            from mutagen.mp4 import MP4
            audio = MP4(path_to_use)
            changed = False
            if record.get("tag_title"):
                audio["\xa9nam"] = [record["tag_title"]]
                changed = True
            elif "\xa9nam" in audio:
                del audio["\xa9nam"]
                changed = True
            if record.get("tag_year"):
                audio["\xa9day"] = [str(record["tag_year"])]
                changed = True
            elif "\xa9day" in audio:
                del audio["\xa9day"]
                changed = True
            if changed:
                audio.save()

        result["restored_path"] = path_to_use

        with MOVIE_STATE_LOCK:
            if current_path in MOVIE_STATE["videos"]:
                del MOVIE_STATE["videos"][current_path]
            refreshed = read_video(path_to_use)
            MOVIE_STATE["videos"][refreshed.path] = refreshed
            if current_path in MOVIE_STATE["order"]:
                idx = MOVIE_STATE["order"].index(current_path)
                MOVIE_STATE["order"][idx] = refreshed.path
            MOVIE_STATE["undo_by_path"].pop(current_path, None)

    except Exception as e:
        result["error"] = str(e)
    return result


@app.route("/api/movies/undo_all", methods=["POST"])
def api_movies_undo_all():
    with MOVIE_STATE_LOCK:
        records = list(MOVIE_STATE["undo_by_path"].values())

    results = [_undo_one_movie(r) for r in records]
    succeeded = sum(1 for r in results if not r["error"])
    return jsonify({"restored": succeeded, "results": results})


@app.route("/api/movies/duplicates/scan", methods=["POST"])
def api_movies_duplicates_scan():
    with MOVIE_STATE_LOCK:
        videos = [MOVIE_STATE["videos"][p] for p in MOVIE_STATE["order"]]

    if not videos:
        return jsonify({"error": "Scan a folder first."}), 400

    exact = dedup.find_exact_duplicates(videos)
    probable = dedup.find_probable_duplicates_movies(videos)
    return jsonify({"exact": exact, "probable": probable})


@app.route("/api/movies/duplicates/quarantine", methods=["POST"])
def api_movies_duplicates_quarantine():
    data = request.get_json(force=True) or {}
    paths = data.get("paths") or []

    with MOVIE_STATE_LOCK:
        folder = MOVIE_STATE["folder"]

    if not folder:
        return jsonify({"error": "Scan a folder first."}), 400
    if not paths:
        return jsonify({"error": "No files selected."}), 400

    # Sweep up any .nfo/poster sidecars sitting next to the flagged video so
    # they move together instead of leaving orphaned sidecars behind.
    all_paths = []
    for p in paths:
        all_paths.append(p)
        base = os.path.splitext(p)[0]
        for suffix in (".nfo", "-poster.jpg"):
            sidecar = base + suffix
            if os.path.exists(sidecar):
                all_paths.append(sidecar)

    results = dedup.quarantine(all_paths, folder)

    with MOVIE_STATE_LOCK:
        for r in results:
            if not r["error"] and r["original_path"] in MOVIE_STATE["videos"]:
                del MOVIE_STATE["videos"][r["original_path"]]
                if r["original_path"] in MOVIE_STATE["order"]:
                    MOVIE_STATE["order"].remove(r["original_path"])
                MOVIE_STATE["undo_by_path"].pop(r["original_path"], None)

    moved = sum(1 for r in results if not r["error"])
    return jsonify({"moved": moved, "results": results})


@app.route("/api/movies/export_csv")
def api_movies_export_csv():
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "file_path", "current_title", "current_year",
        "matched_title", "matched_year", "confidence", "tmdb_url",
    ])
    with MOVIE_STATE_LOCK:
        for p in MOVIE_STATE["order"]:
            v = MOVIE_STATE["videos"][p]
            m = v.match or {}
            writer.writerow([
                v.path, v.tag_title or v.guess_title or "", v.tag_year or v.guess_year or "",
                m.get("title", ""), m.get("year", ""), m.get("confidence", ""), m.get("tmdb_url", ""),
            ])
    mem = io.BytesIO(buf.getvalue().encode("utf-8"))
    mem.seek(0)
    return send_file(mem, mimetype="text/csv", as_attachment=True, download_name="metamatch_movies_report.csv")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=True)
