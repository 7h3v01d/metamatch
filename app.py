"""
app.py
Local web UI for MetaMatch. Run with `python app.py`, then open
http://127.0.0.1:5050 in a browser.

Flow:
  1. POST /api/scan            - scan a folder for audio files
  2. POST /api/match/start     - kick off background MusicBrainz matching
  3. GET  /api/match/progress  - poll matching progress
  4. GET  /api/tracks          - fetch current track + match data
  5. POST /api/apply           - tag/rename a single file
  6. POST /api/apply_all       - tag/rename every file above a confidence bar
  7. GET  /api/export_csv      - download a CSV report of matches
"""

from __future__ import annotations

import csv
import io
import os
import threading

from flask import Flask, jsonify, request, render_template, send_file, Response

from core.scanner import scan_folder, TrackFile
from core.matcher import match_tracks
from core.tagger import apply_match

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
}
STATE_LOCK = threading.Lock()


def _tracks_payload() -> list[dict]:
    with STATE_LOCK:
        return [STATE["tracks"][p].to_dict() for p in STATE["order"]]


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


@app.route("/api/apply", methods=["POST"])
def api_apply():
    data = request.get_json(force=True) or {}
    track_id = data.get("id")
    do_tag = bool(data.get("tag", True))
    do_rename = bool(data.get("rename", True))

    with STATE_LOCK:
        track = STATE["tracks"].get(track_id)

    if not track:
        return jsonify({"error": "Unknown track."}), 404
    if not track.match:
        return jsonify({"error": "This track has no match to apply."}), 400

    result = apply_match(track.path, track.match, do_tag=do_tag, do_rename=do_rename)

    if result["new_path"] != track.path:
        with STATE_LOCK:
            del STATE["tracks"][track.path]
            new_track = scan_and_reread(result["new_path"])
            STATE["tracks"][new_track.path] = new_track
            idx = STATE["order"].index(track.path)
            STATE["order"][idx] = new_track.path
            new_track.match = track.match

    return jsonify(result)


def scan_and_reread(path: str) -> TrackFile:
    from core.scanner import read_track
    return read_track(path)


@app.route("/api/apply_all", methods=["POST"])
def api_apply_all():
    data = request.get_json(force=True) or {}
    do_tag = bool(data.get("tag", True))
    do_rename = bool(data.get("rename", True))
    min_confidence = float(data.get("min_confidence", 75))

    with STATE_LOCK:
        candidates = [STATE["tracks"][p] for p in STATE["order"]]

    results = []
    for track in candidates:
        if not track.match or track.match.get("confidence", 0) < min_confidence:
            continue
        result = apply_match(track.path, track.match, do_tag=do_tag, do_rename=do_rename)
        results.append(result)
        if result["new_path"] != track.path and not result["error"]:
            with STATE_LOCK:
                if track.path in STATE["tracks"]:
                    del STATE["tracks"][track.path]
                new_track = scan_and_reread(result["new_path"])
                new_track.match = track.match
                STATE["tracks"][new_track.path] = new_track
                if track.path in STATE["order"]:
                    idx = STATE["order"].index(track.path)
                    STATE["order"][idx] = new_track.path

    return jsonify({"applied": len(results), "results": results})


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


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=True)
