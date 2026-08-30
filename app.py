"""
app.py
Local web UI for MetaMatch. Run with `python app.py`, then open
http://127.0.0.1:5050 in a browser.

This file is intentionally thin: all the actual scan/match/apply/undo/
dedup logic lives in the metamatch package (metamatch/library.py,
MusicLibrary and MovieLibrary), which has no Flask dependency and can be
used directly - `from metamatch import MusicLibrary` - in a script,
notebook, CLI, or another application entirely. This file's only job is
translating HTTP requests into calls against one shared MusicLibrary and
one shared MovieLibrary instance (this is a local single-user tool, so
one instance of each for the process's lifetime is enough - a
multi-user host application would instead create one pair per
session/user).

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

import io
import os

from flask import Flask, jsonify, request, render_template, send_file, Response

from metamatch import MusicLibrary, MovieLibrary, TvLibrary
from metamatch.art import fetch_cover_art
from metamatch.movie_matcher import TmdbNotConfigured
from metamatch import config as app_config
from metamatch.journal import Journal, RECOVERY_REQUIRED, INTERRUPTED

app = Flask(__name__)

# One shared session per process - see the module docstring for why a
# multi-user host would want one pair of these per user/session instead.
music_library = MusicLibrary()
movie_library = MovieLibrary(journal=music_library.journal)  # one shared journal file for both
tv_library = TvLibrary(journal=music_library.journal)        # same shared journal (kind "tv")


@app.before_request
def _reject_cross_origin_mutations():
    """This API has no auth token - the only thing standing between a
    malicious page open in the same browser and destructive local file
    operations (apply/quarantine/undo) is that a cross-origin fetch/XHR
    carries an Origin header that won't match this server. Reject any
    state-changing request whose Origin doesn't match our own host.
    A missing Origin (curl, same-origin navigation, non-browser clients)
    is allowed through - this specifically closes the browser-CSRF path,
    not every conceivable local-network scenario."""
    if request.method == "GET":
        return None
    origin = request.headers.get("Origin")
    if origin:
        from urllib.parse import urlparse
        if urlparse(origin).netloc != request.host:
            return jsonify({"error": "Cross-origin requests are not allowed."}), 403
    return None


@app.route("/")
def index():
    return render_template("index.html")


DRIVES_SENTINEL = "__drives__"


def _list_windows_drives() -> list[str]:
    """Every drive letter with something mounted at it, e.g. ['C:\\\\', 'D:\\\\']."""
    if os.name != "nt":
        return []
    import string
    return [f"{letter}:\\" for letter in string.ascii_uppercase if os.path.exists(f"{letter}:\\")]


@app.route("/api/browse")
def api_browse():
    """Lists subdirectories of a path, for the folder-picker in the UI.

    This is a local single-user tool where /api/scan already accepts any
    filesystem path the caller names - browsing doesn't expose anything
    scan didn't already implicitly allow, it's just a friendlier way to
    find one. Read-only: no file contents, directory names only, and
    entries this process can't access are skipped rather than raising.

    On Windows there's no single filesystem root - C:\\'s parent is
    itself - so navigating "up" from a drive root instead surfaces a
    virtual "This PC" listing of every other drive letter, requested with
    the DRIVES_SENTINEL path.
    """
    requested = (request.args.get("path") or "").strip()

    if requested == DRIVES_SENTINEL:
        return jsonify({
            "path": "This PC", "parent": None,
            "directories": _list_windows_drives(), "is_drive_list": True,
        })

    current = os.path.abspath(requested) if requested else os.path.expanduser("~")

    if not os.path.isdir(current):
        # Fall back to home rather than erroring, so a stale/invalid path
        # saved from a previous session doesn't strand the picker.
        current = os.path.expanduser("~")

    entries = []
    try:
        with os.scandir(current) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."):
                        entries.append(entry.name)
                except OSError:
                    continue  # unreadable entry (permissions, broken symlink, etc.) - skip it
    except OSError as e:
        return jsonify({"error": f"Can't read that folder: {e}"}), 400

    entries.sort(key=str.lower)
    parent = os.path.dirname(current)
    if parent == current:
        # At a drive root (Windows) or the filesystem root (POSIX). On
        # Windows with more than one drive present, offer the drive list
        # as "up" instead of a dead end.
        parent = DRIVES_SENTINEL if len(_list_windows_drives()) > 1 else None

    return jsonify({
        "path": current,
        "parent": parent,
        "directories": entries,
        "is_drive_list": False,
    })


def _recovery_severity(status: str) -> str:
    """RECOVERY_REQUIRED means a file may be left inconsistent and wants a
    human; everything else recovery surfaces (a benign INTERRUPTED pending
    row) is informational."""
    return "attention" if status == RECOVERY_REQUIRED else "info"


def _recovery_message(notice: dict) -> str:
    name = os.path.basename(notice.get("original_path") or notice.get("current_path") or "a file")
    if notice.get("status") == RECOVERY_REQUIRED:
        info = notice.get("rollback_info") or {}
        note = info.get("note")
        base = (f"{name} may have been left partially changed and couldn't be automatically "
                f"restored — check its tags, filename, and any .nfo/poster sidecars by hand.")
        return f"{base} ({note})" if note else base
    return (f"MetaMatch was closed before it finished working on {name}. Nothing was necessarily "
            f"changed, but its last operation didn't complete — a quick check doesn't hurt.")


def _enrich_notices(notices: list[dict]) -> list[dict]:
    out = []
    for n in notices:
        out.append({**n, "severity": _recovery_severity(n.get("status")),
                    "message": _recovery_message(n)})
    return out


@app.route("/api/recovery")
def api_recovery():
    """Operations that may not have finished cleanly. Two layers:

      * `music`/`movies`: what THIS process's startup recovery found (benign
        interrupted rows plus anything escalated), each tagged with a
        severity and a plain-language message.
      * `needs_attention`: every transaction still sitting at
        RECOVERY_REQUIRED in the journal, regardless of which boot flagged
        it - so a file that genuinely needs a human keeps being surfaced on
        every restart until it's resolved, instead of scrolling past once.
    """
    music_notices = _enrich_notices(music_library.get_recovery_notices())
    movie_notices = _enrich_notices(movie_library.get_recovery_notices())
    tv_notices = _enrich_notices(tv_library.get_recovery_notices())

    attention = (
        [{**n, "kind": "music"} for n in music_library.get_outstanding_recovery()] +
        [{**n, "kind": "movie"} for n in movie_library.get_outstanding_recovery()] +
        [{**n, "kind": "tv"} for n in tv_library.get_outstanding_recovery()]
    )
    interrupted = sum(1 for n in (music_notices + movie_notices + tv_notices) if n["severity"] == "info")

    return jsonify({
        "music": music_notices,
        "movies": movie_notices,
        "tv": tv_notices,
        "needs_attention": attention,
        "summary": {
            "interrupted": interrupted,
            "recovery_required": len(attention),
            "needs_attention": len(attention) > 0,
        },
    })


# ---------------------------------------------------------------------------
# Music
# ---------------------------------------------------------------------------

@app.route("/api/scan", methods=["POST"])
def api_scan():
    data = request.get_json(force=True) or {}
    folder = (data.get("folder") or "").strip()
    recursive = bool(data.get("recursive", True))

    if not folder:
        return jsonify({"error": "Please provide a folder path."}), 400

    try:
        tracks = music_library.scan(folder, recursive=recursive)
    except NotADirectoryError:
        return jsonify({"error": f"'{folder}' is not a folder that exists on this machine."}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"folder": folder, "count": len(tracks), "tracks": tracks})


@app.route("/api/match/start", methods=["POST"])
def api_match_start():
    try:
        music_library.match_async()
    except (ValueError, RuntimeError) as e:
        status = 409 if isinstance(e, RuntimeError) else 400
        return jsonify({"error": str(e)}), status
    return jsonify({"started": True})


@app.route("/api/match/progress")
def api_match_progress():
    return jsonify(music_library.match_progress_snapshot())


@app.route("/api/tracks")
def api_tracks():
    return jsonify({"folder": music_library.folder, "tracks": music_library.tracks_payload()})


@app.route("/api/art/<path:release_id>")
def api_art(release_id):
    """Proxies a small cover-art thumbnail so the browser can show it without
    the Cover Art Archive needing CORS headers for our origin."""
    result = fetch_cover_art(release_id, size="250")
    if not result:
        return "", 404
    image_bytes, mime = result
    return Response(image_bytes, mimetype=mime)


@app.route("/api/apply", methods=["POST"])
def api_apply():
    data = request.get_json(force=True) or {}
    track_id = data.get("id")
    do_tag = bool(data.get("tag", True))
    do_rename = bool(data.get("rename", True))
    do_art = bool(data.get("art", False))

    try:
        result = music_library.apply(track_id, do_tag=do_tag, do_rename=do_rename, do_art=do_art)
    except KeyError:
        return jsonify({"error": "Unknown track."}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(result)


@app.route("/api/apply_all", methods=["POST"])
def api_apply_all():
    data = request.get_json(force=True) or {}
    try:
        result = music_library.apply_all(
            do_tag=bool(data.get("tag", True)),
            do_rename=bool(data.get("rename", True)),
            do_art=bool(data.get("art", False)),
            min_confidence=float(data.get("min_confidence", 75)),
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(result)


@app.route("/api/undo", methods=["POST"])
def api_undo():
    data = request.get_json(force=True) or {}
    try:
        result = music_library.undo(data.get("id"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if result["error"]:
        return jsonify(result), 500
    return jsonify(result)


@app.route("/api/undo_all", methods=["POST"])
def api_undo_all():
    return jsonify(music_library.undo_all())


@app.route("/api/duplicates/scan", methods=["POST"])
def api_duplicates_scan():
    try:
        return jsonify(music_library.find_duplicates())
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/duplicates/quarantine", methods=["POST"])
def api_duplicates_quarantine():
    data = request.get_json(force=True) or {}
    try:
        result = music_library.quarantine(data.get("paths") or [])
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(result)


@app.route("/api/export_csv")
def api_export_csv():
    csv_text = music_library.export_csv()
    mem = io.BytesIO(csv_text.encode("utf-8"))
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
        "ffmpeg_available": movie_library.ffmpeg_available,
        "ffprobe_available": movie_library.ffprobe_available,
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

    try:
        videos = movie_library.scan(folder, recursive=recursive)
    except NotADirectoryError:
        return jsonify({"error": f"'{folder}' is not a folder that exists on this machine."}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "folder": folder, "count": len(videos), "videos": videos,
        "ffprobe_available": movie_library.ffprobe_available,
    })


@app.route("/api/movies/match/start", methods=["POST"])
def api_movies_match_start():
    try:
        movie_library.match_async()
    except TmdbNotConfigured as e:
        return jsonify({"error": str(e)}), 400
    except (ValueError, RuntimeError) as e:
        status = 409 if isinstance(e, RuntimeError) else 400
        return jsonify({"error": str(e)}), status
    return jsonify({"started": True})


@app.route("/api/movies/match/progress")
def api_movies_match_progress():
    return jsonify(movie_library.match_progress_snapshot())


@app.route("/api/movies")
def api_movies_list():
    return jsonify({"folder": movie_library.folder, "videos": movie_library.videos_payload()})


@app.route("/api/movies/apply", methods=["POST"])
def api_movies_apply():
    data = request.get_json(force=True) or {}
    try:
        result = movie_library.apply(
            data.get("id"),
            do_tag=bool(data.get("tag", False)),
            do_rename=bool(data.get("rename", True)),
            do_nfo=bool(data.get("nfo", True)),
            do_poster=bool(data.get("poster", True)),
        )
    except KeyError:
        return jsonify({"error": "Unknown file."}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(result)


@app.route("/api/movies/apply_all", methods=["POST"])
def api_movies_apply_all():
    data = request.get_json(force=True) or {}
    try:
        result = movie_library.apply_all(
            do_tag=bool(data.get("tag", False)),
            do_rename=bool(data.get("rename", True)),
            do_nfo=bool(data.get("nfo", True)),
            do_poster=bool(data.get("poster", True)),
            min_confidence=float(data.get("min_confidence", 75)),
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(result)


@app.route("/api/movies/undo", methods=["POST"])
def api_movies_undo():
    data = request.get_json(force=True) or {}
    try:
        result = movie_library.undo(data.get("id"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if result["error"]:
        return jsonify(result), 500
    return jsonify(result)


@app.route("/api/movies/undo_all", methods=["POST"])
def api_movies_undo_all():
    return jsonify(movie_library.undo_all())


@app.route("/api/movies/duplicates/scan", methods=["POST"])
def api_movies_duplicates_scan():
    try:
        return jsonify(movie_library.find_duplicates())
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/movies/duplicates/quarantine", methods=["POST"])
def api_movies_duplicates_quarantine():
    data = request.get_json(force=True) or {}
    try:
        result = movie_library.quarantine(data.get("paths") or [])
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(result)


@app.route("/api/movies/export_csv")
def api_movies_export_csv():
    csv_text = movie_library.export_csv()
    mem = io.BytesIO(csv_text.encode("utf-8"))
    mem.seek(0)
    return send_file(mem, mimetype="text/csv", as_attachment=True, download_name="metamatch_movies_report.csv")


# --------------------------------------------------------------------- TV
# The episode analogue of the movie routes above. Same shapes, backed by
# tv_library (journal kind "tv"); episodes carry series/season/episode and
# .nfo/-thumb.jpg sidecars instead of a movie title/poster.

@app.route("/api/tv/scan", methods=["POST"])
def api_tv_scan():
    data = request.get_json(force=True) or {}
    folder = (data.get("folder") or "").strip()
    recursive = bool(data.get("recursive", True))
    if not folder:
        return jsonify({"error": "No folder provided."}), 400
    try:
        episodes = tv_library.scan(folder, recursive=recursive)
    except NotADirectoryError:
        return jsonify({"error": f"'{folder}' is not a folder that exists on this machine."}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({
        "folder": folder, "count": len(episodes), "episodes": episodes,
        "ffprobe_available": tv_library.ffprobe_available,
    })


@app.route("/api/tv/match/start", methods=["POST"])
def api_tv_match_start():
    try:
        tv_library.match_async()
    except TmdbNotConfigured as e:
        return jsonify({"error": str(e)}), 400
    except (ValueError, RuntimeError) as e:
        status = 409 if isinstance(e, RuntimeError) else 400
        return jsonify({"error": str(e)}), status
    return jsonify({"started": True})


@app.route("/api/tv/match/progress")
def api_tv_match_progress():
    return jsonify(tv_library.match_progress_snapshot())


@app.route("/api/tv")
def api_tv_list():
    return jsonify({"folder": tv_library.folder, "episodes": tv_library.episodes_payload()})


@app.route("/api/tv/apply", methods=["POST"])
def api_tv_apply():
    data = request.get_json(force=True) or {}
    try:
        result = tv_library.apply(
            data.get("id"),
            do_tag=bool(data.get("tag", False)),
            do_rename=bool(data.get("rename", True)),
            do_nfo=bool(data.get("nfo", True)),
            do_thumb=bool(data.get("thumb", True)),
        )
    except KeyError:
        return jsonify({"error": "Unknown file."}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(result)


@app.route("/api/tv/apply_all", methods=["POST"])
def api_tv_apply_all():
    data = request.get_json(force=True) or {}
    try:
        result = tv_library.apply_all(
            do_tag=bool(data.get("tag", False)),
            do_rename=bool(data.get("rename", True)),
            do_nfo=bool(data.get("nfo", True)),
            do_thumb=bool(data.get("thumb", True)),
            min_confidence=float(data.get("min_confidence", 75)),
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(result)


@app.route("/api/tv/undo", methods=["POST"])
def api_tv_undo():
    data = request.get_json(force=True) or {}
    try:
        result = tv_library.undo(data.get("id"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if result["error"]:
        return jsonify(result), 500
    return jsonify(result)


@app.route("/api/tv/undo_all", methods=["POST"])
def api_tv_undo_all():
    return jsonify(tv_library.undo_all())


@app.route("/api/tv/duplicates/scan", methods=["POST"])
def api_tv_duplicates_scan():
    try:
        return jsonify(tv_library.find_duplicates())
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/tv/duplicates/quarantine", methods=["POST"])
def api_tv_duplicates_quarantine():
    data = request.get_json(force=True) or {}
    try:
        result = tv_library.quarantine(data.get("paths") or [])
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(result)


@app.route("/api/tv/export_csv")
def api_tv_export_csv():
    csv_text = tv_library.export_csv()
    mem = io.BytesIO(csv_text.encode("utf-8"))
    mem.seek(0)
    return send_file(mem, mimetype="text/csv", as_attachment=True, download_name="metamatch_tv_report.csv")


@app.route("/api/tv/series_metadata", methods=["POST"])
def api_tv_series_metadata():
    data = request.get_json(force=True) or {}
    try:
        result = tv_library.write_series_metadata(
            min_confidence=float(data.get("min_confidence", 75)),
            do_poster=bool(data.get("poster", True)),
            do_season_posters=bool(data.get("season_posters", True)),
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(result)


@app.route("/api/tv/series_metadata/undo", methods=["POST"])
def api_tv_series_metadata_undo():
    return jsonify(tv_library.undo_series_metadata_all())


if __name__ == "__main__":
    import os as _os
    # debug=True enables Werkzeug's interactive debugger, which allows
    # arbitrary code execution from anyone who can reach it - a real risk
    # if this ever ends up bound to more than loopback (a misconfigured
    # proxy, a change to host=, etc.). Off by default; opt in explicitly
    # for local development with METAMATCH_DEBUG=1.
    debug_mode = _os.environ.get("METAMATCH_DEBUG") == "1"
    app.run(host="127.0.0.1", port=5050, debug=debug_mode)
