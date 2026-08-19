"""
library.py
The framework-agnostic core of MetaMatch: MusicLibrary and MovieLibrary.

These classes hold no web-framework dependency and no purely-in-memory
undo state - each instance owns its own scanned files and match results
in memory, but apply/undo history is backed by a persistent write-ahead
journal (see journal.py), so undo survives a restart and a crash
mid-operation is detectable rather than silently forgotten.

    from metamatch import MusicLibrary

    lib = MusicLibrary()
    lib.scan("/path/to/music")
    lib.match()  # synchronous; pass progress_callback for background use
    for track in lib.tracks_payload():
        if track.get("match", {}).get("confidence", 0) >= 85:
            lib.apply(track["id"], do_tag=True, do_rename=True)

app.py wraps one of each in a Flask app and translates HTTP requests into
calls against them - see app.py for that thin adapter layer. Movies work
the same way via MovieLibrary, with the addition of a TMDB API key (see
metamatch/config.py) and .nfo/poster sidecars instead of embedded art.
"""

from __future__ import annotations

import base64
import csv
import io
import math
import os
import threading
from typing import Callable, Optional

from . import scanner as scanner_module
from . import matcher as matcher_module
from . import tagger as tagger_module
from . import art as art_module
from . import dedup as dedup_module

from . import video_scanner as video_scanner_module
from . import movie_matcher as movie_matcher_module
from . import movie_tagger as movie_tagger_module
from . import config as config_module
from . import journal as journal_module

# Cap on how large a pre-existing sidecar we'll snapshot in memory (and in
# the journal) for undo purposes. .nfo files are small XML/text and always
# well under this; posters occasionally aren't (a very high-res poster
# someone placed by hand), in which case undo falls back to leaving the
# file alone rather than restoring its exact bytes - see MovieLibrary._undo_txn.
_MAX_SIDECAR_SNAPSHOT_BYTES = 8 * 1024 * 1024

ProgressCallback = Optional[Callable[[int, int], None]]


def _csv_safe(value) -> object:
    """Neutralizes spreadsheet formula injection: a cell that starts with
    =, +, -, or @ is interpreted as a formula by Excel/Sheets/LibreOffice
    when the CSV is opened, which is a real risk here since these values
    can originate from filenames or metadata pulled from MusicBrainz/TMDB
    (i.e., untrusted external text), not just data the user typed."""
    if isinstance(value, str) and value and value[0] in ("=", "+", "-", "@"):
        return "'" + value
    return value


def _validate_confidence(min_confidence: float) -> float:
    """Rejects non-finite or out-of-range thresholds rather than letting them
    silently defeat the filter - `x < float('nan')` is always False in
    Python, so an unvalidated NaN threshold would bypass filtering entirely
    and apply to every match regardless of confidence."""
    try:
        value = float(min_confidence)
    except (TypeError, ValueError):
        raise ValueError(f"min_confidence must be a number, got {min_confidence!r}")
    if not math.isfinite(value):
        raise ValueError(f"min_confidence must be a finite number, got {min_confidence!r}")
    if not (0 <= value <= 100):
        raise ValueError(f"min_confidence must be between 0 and 100, got {value}")
    return value


def _read_small_file(path: str, max_bytes: int) -> Optional[bytes]:
    """Reads a file's full contents if it exists and isn't larger than max_bytes, else None."""
    try:
        if not os.path.isfile(path) or os.path.getsize(path) > max_bytes:
            return None
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _fingerprint_changed(path: str, expected_size: Optional[int], expected_mtime_ns: Optional[int]) -> bool:
    """True if the file at path no longer matches the size/mtime recorded
    at scan time - i.e. something replaced or modified it since MetaMatch
    last looked. Applying a match found for the old content to whatever
    is there now would silently mislabel an unrelated file, so callers
    should refuse to proceed rather than guess."""
    if expected_size is None or expected_mtime_ns is None:
        return False  # no fingerprint recorded (e.g. constructed directly in a test) - nothing to check
    try:
        current = os.stat(path)
    except OSError:
        return True  # file's gone entirely - definitely changed
    return current.st_size != expected_size or current.st_mtime_ns != expected_mtime_ns


def _b64_or_none(data: Optional[bytes]) -> Optional[str]:
    return base64.b64encode(data).decode("ascii") if data is not None else None


def _unb64_or_none(data: Optional[str]) -> Optional[bytes]:
    return base64.b64decode(data) if data is not None else None


class MusicLibrary:
    """One scanned folder of audio files, its MusicBrainz matches, and
    journal-backed undo history (see journal.py - undo persists across
    restarts and a crash mid-apply is detectable via get_recovery_notices())."""

    def __init__(self, journal: Optional[journal_module.Journal] = None):
        self.folder: Optional[str] = None
        self.tracks: dict[str, scanner_module.TrackFile] = {}
        self.order: list[str] = []
        self.match_progress: dict = {"running": False, "done": 0, "total": 0}
        self._lock = threading.RLock()

        self.journal = journal or journal_module.Journal(journal_module.DEFAULT_JOURNAL_PATH)
        # Any transaction still "pending" here means the process died
        # between starting and finishing an apply/undo last time this
        # journal was used - surfaced via get_recovery_notices() rather
        # than silently dropped.
        self.recovered_transactions = self.journal.recover("music")

    def get_recovery_notices(self) -> list[dict]:
        """Transactions that were left mid-flight by a previous crash,
        discovered when this instance was constructed. Each one names a
        file that *may* have been partially modified - worth a manual
        check, since the interrupted operation's outcome is unknown."""
        return [t.to_dict() for t in self.recovered_transactions]

    # ---------------------------------------------------------------- scan
    def scan(self, folder: str, recursive: bool = True) -> list[dict]:
        """Scans a folder for audio files. Replaces any previously scanned session."""
        tracks = scanner_module.scan_folder(folder, recursive=recursive)
        with self._lock:
            self.folder = folder
            self.tracks = {t.path: t for t in tracks}
            self.order = [t.path for t in tracks]
            self.match_progress = {"running": False, "done": 0, "total": len(tracks)}
        return self.tracks_payload()

    def tracks_payload(self) -> list[dict]:
        """JSON-serializable view of every scanned track, in scan order."""
        with self._lock:
            undoable = self.journal.get_undoable_paths("music", folder=self.folder)
            out = []
            for p in self.order:
                d = self.tracks[p].to_dict()
                d["can_undo"] = p in undoable
                out.append(d)
            return out

    # ------------------------------------------------------------- match
    def match(self, progress_callback: ProgressCallback = None) -> None:
        """Synchronous MusicBrainz matching over every scanned track."""
        with self._lock:
            tracks = [self.tracks[p] for p in self.order]
            self.match_progress = {"running": True, "done": 0, "total": len(tracks)}

        def on_progress(done, total):
            with self._lock:
                self.match_progress["done"] = done
                self.match_progress["total"] = total
            if progress_callback:
                progress_callback(done, total)

        try:
            matcher_module.match_tracks(tracks, progress_callback=on_progress)
        finally:
            with self._lock:
                self.match_progress["running"] = False

    def match_async(self, progress_callback: ProgressCallback = None) -> threading.Thread:
        """Runs match() on a background daemon thread. Raises if nothing scanned or already running."""
        with self._lock:
            if not self.order:
                raise ValueError("Scan a folder first.")
            if self.match_progress["running"]:
                raise RuntimeError("Matching is already running.")
            # Claim "running" atomically with the check above, still inside
            # the lock - otherwise two concurrent callers could both observe
            # running=False before either thread starts, and both launch a
            # matching pass at once.
            self.match_progress = {"running": True, "done": 0, "total": len(self.order)}

        thread = threading.Thread(target=self.match, kwargs={"progress_callback": progress_callback}, daemon=True)
        thread.start()
        return thread

    def match_progress_snapshot(self) -> dict:
        with self._lock:
            return dict(self.match_progress)

    # ----------------------------------------------------------- apply
    def _snapshot_original_tags(self, track: scanner_module.TrackFile) -> dict:
        return {
            "artist": track.tag_artist,
            "title": track.tag_title,
            "album": track.tag_album,
            "date": track.tag_year,
        }

    def apply(self, track_id: str, do_tag: bool = True, do_rename: bool = True, do_art: bool = False) -> dict:
        """Applies the match found for one track: writes tags, embeds cover art, and/or renames the file."""
        with self._lock:
            track = self.tracks.get(track_id)
        if not track:
            raise KeyError(f"Unknown track: {track_id}")
        if not track.match:
            raise ValueError("This track has no match to apply.")
        return self._apply_one(track, do_tag, do_rename, do_art)

    def apply_all(self, do_tag: bool = True, do_rename: bool = True, do_art: bool = False,
                  min_confidence: float = 75.0) -> dict:
        """Applies every scanned track whose match confidence is at or above min_confidence."""
        min_confidence = _validate_confidence(min_confidence)
        with self._lock:
            candidates = [self.tracks[p] for p in self.order]

        results = []
        for track in candidates:
            if not track.match or track.match.get("confidence", 0) < min_confidence:
                continue
            results.append(self._apply_one(track, do_tag, do_rename, do_art))

        succeeded = sum(1 for r in results if not r["error"])
        return {
            "attempted": len(results), "succeeded": succeeded, "failed": len(results) - succeeded,
            "results": results,
        }

    def _apply_one(self, track: scanner_module.TrackFile, do_tag: bool, do_rename: bool, do_art: bool) -> dict:
        if _fingerprint_changed(track.path, track.size_bytes, track.mtime_ns):
            return {
                "original_path": track.path, "new_path": track.path,
                "tagged": False, "renamed": False, "art_embedded": False,
                "error": "File changed on disk since it was scanned/matched - rescan before applying.",
            }

        # If this exact path already has an active (committed, not yet
        # undone) journal transaction, chain from ITS original state
        # instead of re-snapshotting the current (already-modified) tags -
        # otherwise a second Apply on the same file would silently make
        # its already-modified state look like the "original" to undo back to.
        existing = self.journal.get_active_for_path("music", track.path)
        if existing:
            true_original_path = existing.original_path
            before_state = existing.before_state
        else:
            true_original_path = track.path
            before_state = self._snapshot_original_tags(track)

        operation = {"do_tag": do_tag, "do_rename": do_rename, "do_art": do_art}
        txn_id = self.journal.begin("music", true_original_path, track.path, before_state, operation)

        art_bytes = art_mime = None
        if do_art and track.match and track.match.get("release_id"):
            fetched = art_module.fetch_cover_art(track.match["release_id"], size="500")
            if fetched:
                art_bytes, art_mime = fetched

        result = tagger_module.apply_match(
            track.path, track.match, do_tag=do_tag, do_rename=do_rename,
            do_art=do_art, art_bytes=art_bytes, art_mime=art_mime,
        )

        if result["error"]:
            self.journal.fail(txn_id, result["error"])
        else:
            self.journal.commit(txn_id, result["new_path"])
            if existing:
                self.journal.mark_superseded(existing.id)

            with self._lock:
                if track.path in self.tracks:
                    del self.tracks[track.path]
                refreshed = scanner_module.read_track(result["new_path"])
                refreshed.match = track.match
                self.tracks[refreshed.path] = refreshed
                if track.path in self.order:
                    idx = self.order.index(track.path)
                    self.order[idx] = refreshed.path

        return result

    # ------------------------------------------------------------ undo
    def undo(self, track_id: str) -> dict:
        txn = self.journal.get_active_for_path("music", track_id)
        if not txn:
            raise ValueError("Nothing to undo for this file.")
        return self._undo_txn(txn)

    def undo_all(self) -> dict:
        txns = self.journal.list_undoable("music", folder=self.folder)
        results = [self._undo_txn(t) for t in txns]
        succeeded = sum(1 for r in results if not r["error"])
        return {"restored": succeeded, "results": results}

    def _undo_txn(self, txn: journal_module.Transaction) -> dict:
        current_path = txn.current_path
        original_path = txn.original_path
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

            tagger_module.set_or_clear_tags(path_to_tag, **txn.before_state)
            result["restored_path"] = path_to_tag
            self.journal.mark_rolled_back(txn.id)

            with self._lock:
                if current_path in self.tracks:
                    del self.tracks[current_path]
                refreshed = scanner_module.read_track(path_to_tag)
                self.tracks[refreshed.path] = refreshed
                if current_path in self.order:
                    idx = self.order.index(current_path)
                    self.order[idx] = refreshed.path

        except Exception as e:
            result["error"] = str(e)
        return result

    # ------------------------------------------------------- duplicates
    def find_duplicates(self) -> dict:
        with self._lock:
            tracks = [self.tracks[p] for p in self.order]
        if not tracks:
            raise ValueError("Scan a folder first.")
        return {
            "exact": dedup_module.find_exact_duplicates(tracks),
            "probable": dedup_module.find_probable_duplicates(tracks),
        }

    def quarantine(self, paths: list[str]) -> dict:
        with self._lock:
            folder = self.folder
            valid_paths = [p for p in paths if p in self.tracks]
            fingerprints = {p: (self.tracks[p].size_bytes, self.tracks[p].mtime_ns) for p in valid_paths}

        if not folder:
            raise ValueError("Scan a folder first.")
        if not paths:
            raise ValueError("No files selected.")

        rejected = [p for p in paths if p not in valid_paths]
        results = []

        # A file that's tracked but no longer matches what was recorded at
        # scan time (replaced/modified since) is refused rather than moved -
        # the same TOCTOU guard apply() already uses, applied here too so
        # quarantine can't be tricked into moving unrelated content that
        # happens to now sit at a previously-scanned path.
        still_valid = []
        for p in valid_paths:
            size, mtime_ns = fingerprints[p]
            if _fingerprint_changed(p, size, mtime_ns):
                results.append({
                    "original_path": p, "new_path": None,
                    "error": "File changed on disk since it was scanned - rescan before quarantining.",
                })
            elif os.path.isfile(p):
                still_valid.append(p)

        results = dedup_module.quarantine(still_valid, folder) + results
        for p in rejected:
            results.append({
                "original_path": p, "new_path": None,
                "error": "Refused: not a file from the current scan.",
            })

        with self._lock:
            for r in results:
                if not r["error"] and r["original_path"] in self.tracks:
                    del self.tracks[r["original_path"]]
                    if r["original_path"] in self.order:
                        self.order.remove(r["original_path"])

        moved = sum(1 for r in results if not r["error"])
        return {"moved": moved, "results": results}

    # ----------------------------------------------------------- export
    def export_csv(self) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "file_path", "current_artist", "current_title", "current_album",
            "matched_artist", "matched_title", "matched_album", "matched_date",
            "confidence", "musicbrainz_url",
        ])
        with self._lock:
            for p in self.order:
                t = self.tracks[p]
                m = t.match or {}
                writer.writerow([_csv_safe(v) for v in [
                    t.path, t.tag_artist or "", t.tag_title or "", t.tag_album or "",
                    m.get("artist", ""), m.get("title", ""), m.get("album", ""), m.get("date", ""),
                    m.get("confidence", ""), m.get("musicbrainz_url", ""),
                ]])
        return buf.getvalue()


class MovieLibrary:
    """One scanned folder of video files, its TMDB matches, and
    journal-backed undo history (see journal.py)."""

    def __init__(self, journal: Optional[journal_module.Journal] = None):
        self.folder: Optional[str] = None
        self.videos: dict[str, video_scanner_module.VideoFile] = {}
        self.order: list[str] = []
        self.match_progress: dict = {"running": False, "done": 0, "total": 0, "error": None}
        self._lock = threading.RLock()

        self.journal = journal or journal_module.Journal(journal_module.DEFAULT_JOURNAL_PATH)
        self.recovered_transactions = self.journal.recover("movie")

    def get_recovery_notices(self) -> list[dict]:
        return [t.to_dict() for t in self.recovered_transactions]

    @property
    def ffprobe_available(self) -> bool:
        return video_scanner_module.FFPROBE_AVAILABLE

    @property
    def ffmpeg_available(self) -> bool:
        return movie_tagger_module.FFMPEG_AVAILABLE

    # ---------------------------------------------------------------- scan
    def scan(self, folder: str, recursive: bool = True) -> list[dict]:
        videos = video_scanner_module.scan_folder(folder, recursive=recursive)
        with self._lock:
            self.folder = folder
            self.videos = {v.path: v for v in videos}
            self.order = [v.path for v in videos]
            self.match_progress = {"running": False, "done": 0, "total": len(videos), "error": None}
        return self.videos_payload()

    def videos_payload(self) -> list[dict]:
        with self._lock:
            undoable = self.journal.get_undoable_paths("movie", folder=self.folder)
            out = []
            for p in self.order:
                d = self.videos[p].to_dict()
                d["can_undo"] = p in undoable
                out.append(d)
            return out

    # ------------------------------------------------------------- match
    def match(self, progress_callback: ProgressCallback = None) -> None:
        with self._lock:
            videos = [self.videos[p] for p in self.order]
            self.match_progress = {"running": True, "done": 0, "total": len(videos), "error": None}

        def on_progress(done, total):
            with self._lock:
                self.match_progress["done"] = done
                self.match_progress["total"] = total
            if progress_callback:
                progress_callback(done, total)

        error_message = None
        try:
            movie_matcher_module.match_videos(videos, progress_callback=on_progress)
        except movie_matcher_module.TmdbNotConfigured as e:
            error_message = str(e)
        except Exception as e:
            # Any other unexpected failure (network error, bug, etc.) must
            # still release "running" - otherwise the UI polls forever and
            # the library can never be matched again without a rescan.
            error_message = f"Matching failed: {e}"
        finally:
            with self._lock:
                self.match_progress["running"] = False
                self.match_progress["error"] = error_message

    def match_async(self, progress_callback: ProgressCallback = None) -> threading.Thread:
        if not config_module.get_tmdb_api_key():
            raise movie_matcher_module.TmdbNotConfigured("Add a TMDB API key first.")
        with self._lock:
            if not self.order:
                raise ValueError("Scan a folder first.")
            if self.match_progress["running"]:
                raise RuntimeError("Matching is already running.")
            self.match_progress = {"running": True, "done": 0, "total": len(self.order), "error": None}

        thread = threading.Thread(target=self.match, kwargs={"progress_callback": progress_callback}, daemon=True)
        thread.start()
        return thread

    def match_progress_snapshot(self) -> dict:
        with self._lock:
            return dict(self.match_progress)

    # ----------------------------------------------------------- apply
    def _snapshot_original(self, video: video_scanner_module.VideoFile) -> dict:
        base = os.path.splitext(video.path)[0]
        nfo_path = base + ".nfo"
        poster_path = base + "-poster.jpg"

        # Snapshot actual sidecar bytes (not just "did one exist") so undo
        # can restore a pre-existing .nfo/poster's real content, instead of
        # just leaving whatever our own write clobbered it with in place.
        # Bytes are base64-encoded here since the journal stores this as JSON.
        nfo_bytes = _read_small_file(nfo_path, _MAX_SIDECAR_SNAPSHOT_BYTES)
        poster_bytes = _read_small_file(poster_path, _MAX_SIDECAR_SNAPSHOT_BYTES)

        return {
            "tag_title": video.tag_title,
            "tag_year": video.tag_year,
            "had_nfo": os.path.exists(nfo_path),
            "nfo_bytes_b64": _b64_or_none(nfo_bytes),
            "had_poster": os.path.exists(poster_path),
            "poster_bytes_b64": _b64_or_none(poster_bytes),
        }

    def apply(self, video_id: str, do_tag: bool = False, do_rename: bool = True,
              do_nfo: bool = True, do_poster: bool = True) -> dict:
        with self._lock:
            video = self.videos.get(video_id)
        if not video:
            raise KeyError(f"Unknown file: {video_id}")
        if not video.match:
            raise ValueError("This file has no match to apply.")
        return self._apply_one(video, do_tag, do_rename, do_nfo, do_poster)

    def apply_all(self, do_tag: bool = False, do_rename: bool = True, do_nfo: bool = True,
                  do_poster: bool = True, min_confidence: float = 75.0) -> dict:
        min_confidence = _validate_confidence(min_confidence)
        with self._lock:
            candidates = [self.videos[p] for p in self.order]

        results = []
        for video in candidates:
            if not video.match or video.match.get("confidence", 0) < min_confidence:
                continue
            results.append(self._apply_one(video, do_tag, do_rename, do_nfo, do_poster))

        succeeded = sum(1 for r in results if not r["error"])
        return {
            "attempted": len(results), "succeeded": succeeded, "failed": len(results) - succeeded,
            "results": results,
        }

    def _apply_one(self, video: video_scanner_module.VideoFile, do_tag: bool, do_rename: bool,
                    do_nfo: bool, do_poster: bool) -> dict:
        if _fingerprint_changed(video.path, video.size_bytes, video.mtime_ns):
            return {
                "original_path": video.path, "new_path": video.path,
                "tagged": False, "renamed": False, "nfo_path": None, "poster_path": None,
                "error": "File changed on disk since it was scanned/matched - rescan before applying.",
            }

        existing = self.journal.get_active_for_path("movie", video.path)
        if existing:
            true_original_path = existing.original_path
            snapshot = existing.before_state
        else:
            true_original_path = video.path
            snapshot = self._snapshot_original(video)

        operation = {"do_tag": do_tag, "do_rename": do_rename, "do_nfo": do_nfo, "do_poster": do_poster}
        txn_id = self.journal.begin("movie", true_original_path, video.path, snapshot, operation)

        result = movie_tagger_module.apply_movie_match(
            video.path, video.match, do_tag=do_tag, do_rename=do_rename, do_nfo=do_nfo, do_poster=do_poster,
        )

        if result["error"]:
            self.journal.fail(txn_id, result["error"])
        else:
            # Record the EXACT sidecar paths this apply actually produced,
            # not a path undo could reconstruct later - a rename-time name
            # collision can push a sidecar to an alternate suffixed name
            # (see movie_tagger._safe_move), and undo must operate on that
            # real path or risk deleting an unrelated file that happens to
            # sit at the naively-expected name instead.
            after_state = {"nfo_path": result.get("nfo_path"), "poster_path": result.get("poster_path")}
            self.journal.commit(txn_id, result["new_path"], after_state=after_state)
            if existing:
                self.journal.mark_superseded(existing.id)

            with self._lock:
                if video.path in self.videos:
                    del self.videos[video.path]
                refreshed = video_scanner_module.read_video(result["new_path"])
                refreshed.match = video.match
                self.videos[refreshed.path] = refreshed
                if video.path in self.order:
                    idx = self.order.index(video.path)
                    self.order[idx] = refreshed.path

        return result

    # ------------------------------------------------------------ undo
    def undo(self, video_id: str) -> dict:
        txn = self.journal.get_active_for_path("movie", video_id)
        if not txn:
            raise ValueError("Nothing to undo for this file.")
        return self._undo_txn(txn)

    def undo_all(self) -> dict:
        txns = self.journal.list_undoable("movie", folder=self.folder)
        results = [self._undo_txn(t) for t in txns]
        succeeded = sum(1 for r in results if not r["error"])
        return {"restored": succeeded, "results": results}

    def _undo_txn(self, txn: journal_module.Transaction) -> dict:
        current_path = txn.current_path
        original_path = txn.original_path
        snapshot = txn.before_state
        result = {"restored_path": current_path, "error": None}
        try:
            # Use the EXACT sidecar paths recorded when this apply ran
            # (see _apply_one) rather than reconstructing them from the
            # current video filename - a rename-time collision with an
            # unrelated file can push a sidecar to an alternate suffixed
            # name, and guessing at "current basename + .nfo" can land on
            # that unrelated file instead of the one MetaMatch actually
            # created, deleting or overwriting something it never touched.
            after_state = txn.after_state or {}
            has_exact_paths = "nfo_path" in after_state or "poster_path" in after_state
            if has_exact_paths:
                nfo_current = after_state.get("nfo_path")
                poster_current = after_state.get("poster_path")
            else:
                # Transaction predates exact-path tracking (a journal from
                # before this fix). Falls back to the old reconstruction,
                # which is only correct when no rename-time collision
                # occurred - there's no way to recover the real path for
                # transactions written before it was tracked.
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
                if nfo_current and os.path.exists(nfo_current):
                    nfo_current = movie_tagger_module._safe_move(nfo_current, base_original + ".nfo")
                if poster_current and os.path.exists(poster_current):
                    poster_current = movie_tagger_module._safe_move(poster_current, base_original + "-poster.jpg")

            # A sidecar we created fresh (didn't exist before this apply)
            # gets removed on undo. One that already existed gets its
            # original bytes restored if we snapshotted them; if it was
            # too large to snapshot or couldn't be read, it's left alone
            # rather than guessed at. A None path here (do_nfo/do_poster
            # was False, so this transaction never touched that sidecar
            # at all) is skipped entirely - nothing to undo.
            nfo_bytes = _unb64_or_none(snapshot.get("nfo_bytes_b64"))
            poster_bytes = _unb64_or_none(snapshot.get("poster_bytes_b64"))

            if nfo_current:
                if not snapshot.get("had_nfo"):
                    if os.path.exists(nfo_current):
                        os.remove(nfo_current)
                elif nfo_bytes is not None:
                    with open(nfo_current, "wb") as f:
                        f.write(nfo_bytes)

            if poster_current:
                if not snapshot.get("had_poster"):
                    if os.path.exists(poster_current):
                        os.remove(poster_current)
                elif poster_bytes is not None:
                    with open(poster_current, "wb") as f:
                        f.write(poster_bytes)

            # Embedded-tag revert is only reliable for mp4/m4v (direct atom
            # edit, cheap to undo). mkv/avi/mov/wmv go through an ffmpeg
            # remux to embed, which isn't cheaply reversible, so those are
            # left as-is - consistent with how music cover art is handled.
            ext = os.path.splitext(path_to_use)[1].lower()
            if ext in (".mp4", ".m4v"):
                from mutagen.mp4 import MP4
                audio = MP4(path_to_use)
                changed = False
                if snapshot.get("tag_title"):
                    audio["\xa9nam"] = [snapshot["tag_title"]]
                    changed = True
                elif "\xa9nam" in audio:
                    del audio["\xa9nam"]
                    changed = True
                if snapshot.get("tag_year"):
                    audio["\xa9day"] = [str(snapshot["tag_year"])]
                    changed = True
                elif "\xa9day" in audio:
                    del audio["\xa9day"]
                    changed = True
                if changed:
                    audio.save()

            result["restored_path"] = path_to_use
            self.journal.mark_rolled_back(txn.id)

            with self._lock:
                if current_path in self.videos:
                    del self.videos[current_path]
                refreshed = video_scanner_module.read_video(path_to_use)
                self.videos[refreshed.path] = refreshed
                if current_path in self.order:
                    idx = self.order.index(current_path)
                    self.order[idx] = refreshed.path

        except Exception as e:
            result["error"] = str(e)
        return result

    # ------------------------------------------------------- duplicates
    def find_duplicates(self) -> dict:
        with self._lock:
            videos = [self.videos[p] for p in self.order]
        if not videos:
            raise ValueError("Scan a folder first.")
        return {
            "exact": dedup_module.find_exact_duplicates(videos),
            "probable": dedup_module.find_probable_duplicates_movies(videos),
        }

    def quarantine(self, paths: list[str]) -> dict:
        with self._lock:
            folder = self.folder
            valid_paths = [p for p in paths if p in self.videos]
            fingerprints = {p: (self.videos[p].size_bytes, self.videos[p].mtime_ns) for p in valid_paths}

        if not folder:
            raise ValueError("Scan a folder first.")
        if not paths:
            raise ValueError("No files selected.")

        rejected = [p for p in paths if p not in valid_paths]
        results = []

        # Same TOCTOU guard apply() uses: refuse a file that no longer
        # matches what was recorded at scan time, rather than quarantining
        # whatever now happens to sit at that path.
        still_valid = []
        for p in valid_paths:
            size, mtime_ns = fingerprints[p]
            if _fingerprint_changed(p, size, mtime_ns):
                results.append({
                    "original_path": p, "new_path": None,
                    "error": "File changed on disk since it was scanned - rescan before quarantining.",
                })
            elif os.path.isfile(p):
                still_valid.append(p)

        # Sweep up any .nfo/poster sidecars sitting next to a *verified*
        # flagged video so they move together instead of leaving orphans
        # behind. Sidecar paths are always derived here from a video path
        # we ourselves discovered during scan, never taken from the caller.
        all_paths = []
        for p in still_valid:
            all_paths.append(p)
            base = os.path.splitext(p)[0]
            for suffix in (".nfo", "-poster.jpg"):
                sidecar = base + suffix
                if os.path.isfile(sidecar):
                    all_paths.append(sidecar)

        results = dedup_module.quarantine(all_paths, folder) + results
        for p in rejected:
            results.append({
                "original_path": p, "new_path": None,
                "error": "Refused: not a file from the current scan.",
            })

        with self._lock:
            for r in results:
                if not r["error"] and r["original_path"] in self.videos:
                    del self.videos[r["original_path"]]
                    if r["original_path"] in self.order:
                        self.order.remove(r["original_path"])

        moved = sum(1 for r in results if not r["error"])
        return {"moved": moved, "results": results}

    # ----------------------------------------------------------- export
    def export_csv(self) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "file_path", "current_title", "current_year",
            "matched_title", "matched_year", "confidence", "tmdb_url",
        ])
        with self._lock:
            for p in self.order:
                v = self.videos[p]
                m = v.match or {}
                writer.writerow([_csv_safe(val) for val in [
                    v.path, v.tag_title or v.guess_title or "", v.tag_year or v.guess_year or "",
                    m.get("title", ""), m.get("year", ""), m.get("confidence", ""), m.get("tmdb_url", ""),
                ]])
        return buf.getvalue()
