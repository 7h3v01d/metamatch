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
import contextlib
import csv
import io
import math
import os
import stat as stat_module
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
from . import episode_scanner as episode_scanner_module
from . import tv_matcher as tv_matcher_module
from . import tv_tagger as tv_tagger_module
from . import config as config_module
from . import journal as journal_module
from . import fingerprint as fingerprint_module
from . import pathsafe as pathsafe_module

# Cap on how large a pre-existing sidecar we'll snapshot in memory (and in
# the journal) for undo purposes. .nfo files are small XML/text and always
# well under this; posters occasionally aren't (a very high-res poster
# someone placed by hand), in which case undo falls back to leaving the
# file alone rather than restoring its exact bytes - see MovieLibrary._undo_txn.
_MAX_SIDECAR_SNAPSHOT_BYTES = 8 * 1024 * 1024


class _PathLockRegistry:
    """Hands out one lock per canonical file path so a mutating operation
    (Apply / Undo / Quarantine) can hold it for the WHOLE sequence -
    fingerprint check, journal begin, mutate, commit/rollback, refresh -
    making same-file operations mutually exclusive while still allowing
    different files to proceed concurrently.

    This matters because Flask services requests concurrently: without it,
    two Apply requests on the same file can both pass the stale-object check
    and journal 'begin' before either mutates, then race - one renames the
    file out from under the other, whose rollback then fails and manufactures
    a bogus RECOVERY_REQUIRED incident (and concurrent writers to the same
    media container are worse still). The lock is acquired BEFORE the
    stale-object check so a waiting caller re-checks against the file's real
    post-operation state and cleanly aborts ('file changed - rescan') instead
    of carrying stale information through.

    Keyed by realpath so two different pathnames for the same object (e.g. a
    path and its post-rename name) don't get independent locks mid-sequence."""

    def __init__(self):
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()

    def get(self, path: str) -> threading.RLock:
        try:
            key = os.path.realpath(os.path.abspath(path))
        except OSError:
            key = os.path.abspath(path)
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._locks[key] = lock
            return lock


def _authority_error(root: "str | None", path: str, *, allow_missing: bool = False) -> "str | None":
    """Returns a refusal reason if `path` is not a safe mutation target under
    the active library `root`, else None. Used by every destructive entry
    point (Apply/Undo/Quarantine/series metadata) to re-check authority at
    mutation time.

    With a known root, this is the full validate_mutation_target() check. With
    NO root (an Undo issued after a restart before any scan, on a legacy row
    that never recorded its root), it still enforces every property that
    doesn't require knowing the root - reject links/reparse points AND
    hard-link aliases - so restart recovery can't become a weaker authority
    path than Apply. (Newer transactions record library_root, so the rooted
    branch is taken even on restart; this fallback is the legacy safety net.)"""
    if root:
        ok, reason = pathsafe_module.validate_mutation_target(path, root, allow_missing=allow_missing)
        return None if ok else reason

    if pathsafe_module.is_link_or_reparse(path):
        return "it is a symlink or reparse point"
    try:
        st = os.stat(path)
    except OSError:
        if allow_missing and not os.path.lexists(path):
            return None
        return "it is unreadable"
    if stat_module.S_ISREG(st.st_mode) and getattr(st, "st_nlink", 1) > 1:
        return ("it has multiple hard-link names, so MetaMatch can't prove every "
                "alias is inside the library")
    return None


@contextlib.contextmanager
def _locked_paths(registry: _PathLockRegistry, paths):
    """Hold the mutation locks for several paths at once (a bulk Quarantine
    moving many files), acquired in a canonical (sorted realpath) order so two
    concurrent bulk operations can't deadlock by grabbing the same locks in
    opposite orders. Single-file Apply/Undo take one lock each, so they can't
    deadlock against this regardless."""
    keys = sorted({os.path.realpath(os.path.abspath(p)) for p in paths})
    with contextlib.ExitStack() as stack:
        for key in keys:
            stack.enter_context(registry.get(key))
        yield

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


def _validate_margin(min_margin: float) -> float:
    """Same NaN/range guard as confidence, for the auto-apply margin floor. A
    non-finite margin would make `margin < nan` always False and silently
    disable the ambiguity gate."""
    try:
        value = float(min_margin)
    except (TypeError, ValueError):
        raise ValueError(f"min_margin must be a number, got {min_margin!r}")
    if not math.isfinite(value):
        raise ValueError(f"min_margin must be a finite number, got {min_margin!r}")
    if not (0 <= value <= 100):
        raise ValueError(f"min_margin must be between 0 and 100, got {value}")
    return value


def _passes_apply_thresholds(match: Optional[dict], min_confidence: float, min_margin: float) -> bool:
    """Whether a match clears the bulk-apply bar: confidence at or above
    min_confidence AND, when min_margin is set, a clear-enough lead over the
    runner-up. A match with no runner-up (margin is None - only one candidate
    was found) is NOT ambiguous, so it passes any margin requirement; the
    margin gate only rejects genuine near-ties."""
    if not match or match.get("confidence", 0) < min_confidence:
        return False
    if min_margin > 0:
        margin = match.get("margin")
        if margin is not None and margin < min_margin:
            return False
    return True


def _read_small_file(path: str, max_bytes: int) -> Optional[bytes]:
    """Reads a file's full contents if it exists and isn't larger than max_bytes, else None."""
    try:
        if not os.path.isfile(path) or os.path.getsize(path) > max_bytes:
            return None
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _fingerprint_changed(path: str, expected_size: Optional[int], expected_mtime_ns: Optional[int],
                          expected_hash: Optional[str] = None) -> bool:
    """True if the file at path no longer matches what was recorded
    earlier - i.e. something replaced or modified it since MetaMatch last
    looked. Applying a match found for the old content to whatever is
    there now would silently mislabel an unrelated file, so callers
    should refuse to proceed rather than guess.

    Checks size/mtime first (cheap, catches ordinary replacement), then
    the content hash if one was recorded (catches same-size content swaps
    that also happen to preserve mtime - a real bypass of size+mtime
    alone, found by adversarial review). expected_hash is optional so
    fingerprints recorded before content hashing existed still degrade
    gracefully to the size+mtime check rather than failing outright.
    """
    if expected_size is None or expected_mtime_ns is None:
        return False  # no fingerprint recorded (e.g. constructed directly in a test) - nothing to check
    try:
        current = os.stat(path)
    except OSError:
        return True  # file's gone entirely - definitely changed
    if current.st_size != expected_size or current.st_mtime_ns != expected_mtime_ns:
        return True
    if expected_hash is not None:
        current_hash = fingerprint_module.content_fingerprint(path)
        if current_hash != expected_hash:
            return True
    return False


def _file_fingerprint(path: Optional[str]) -> Optional[dict]:
    """(size, mtime_ns, hash) for a file that exists, or None - used to
    record what a successful apply actually produced, so a later undo can
    verify nothing replaced it in the meantime before touching it."""
    if not path:
        return None
    try:
        st = os.stat(path)
        return {
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
            "hash": fingerprint_module.content_fingerprint(path),
        }
    except OSError:
        return None


def _b64_or_none(data: Optional[bytes]) -> Optional[str]:
    return base64.b64encode(data).decode("ascii") if data is not None else None


def _unb64_or_none(data: Optional[str]) -> Optional[bytes]:
    return base64.b64decode(data) if data is not None else None


# --------------------------------------------------------------------------
# Automatic rollback
#
# apply() records a journal transaction and then performs a *sequence* of
# individually-valid file mutations (music: tags -> art -> rename; movie:
# embed -> nfo -> poster -> rename). If one of those raises partway, the
# earlier successful mutations used to be left in place - the journal knew
# something failed but the file was half-changed. These helpers compensate
# that partial work, driving each failed transaction to one of two terminal
# states: ROLLED_BACK (before-state restored) or RECOVERY_REQUIRED (a
# compensation that was expected to work didn't, so a human must look).
#
# A key invariant makes this tractable: rename is the LAST mutation in both
# taggers, so a failed apply never moved the file. Rollback is therefore
# always in-place at the original path - no rename-back, no cross-file
# clobber risk. The only genuinely irreversible mutations are the ones undo
# already declines to touch (an ffmpeg remux, and re-embedding pre-existing
# cover art we couldn't snapshot); those are surfaced as warnings rather
# than treated as a structural failure.
# --------------------------------------------------------------------------

def _journal_unavailable_result(base: dict, exc: Exception) -> dict:
    """A clean, no-mutation error result for when a journal write fails at or
    before mark_applying - i.e. before any file was touched. The operation is
    aborted and nothing on disk changed, which is exactly what we want when
    the journal (our source of truth for undo/recovery) can't be written."""
    r = dict(base)
    r["error"] = (
        f"Couldn't record this operation in MetaMatch's journal ({exc}); "
        "no changes were made to the file, to stay safe."
    )
    return r


def _safe_commit(journal, txn_id: int, new_path: str, result: dict, after_state=None) -> bool:
    """Commit the transaction after a successful on-disk apply. If the journal
    write itself fails (e.g. the disk filled between mutating the file and
    recording it), the file is already correctly updated - so we do NOT treat
    this as an apply failure or roll anything back. Instead we surface it: the
    change stands on disk, but it can't be undone from here and the row stays
    non-terminal, so a restart's recovery pass will flag it for a check.
    Returns True on a clean commit, False if the journal couldn't record it."""
    try:
        journal.commit(txn_id, new_path, after_state=after_state)
        return True
    except Exception as e:
        result["journal_error"] = str(e)
        result["can_undo"] = False
        result.setdefault("warnings", []).append(
            "The change was written to disk, but MetaMatch couldn't record it in its "
            f"journal ({e}). The file is correctly updated; however this change can't be "
            "undone from within MetaMatch, and it may be flagged for a check after a restart."
        )
        return False


def _mark_rolling_back_safely(journal, txn_id: int) -> None:
    """Best-effort status marker before compensation. A failure to write it
    must not stop the actual file rollback from running."""
    try:
        journal.mark_rolling_back(txn_id)
    except Exception:
        pass


def _finalize_failed_apply_safely(journal, txn_id: int, result: dict, restored_ok: bool, warnings: list) -> None:
    """Record a failed apply's rollback outcome, tolerating a journal write
    failure. The file-level compensation has already run by this point; if the
    journal can't record the result, the row is simply left non-terminal so a
    restart escalates it to RECOVERY_REQUIRED - never a crash."""
    try:
        _finalize_failed_apply(journal, txn_id, result, restored_ok, warnings)
    except Exception as e:
        if warnings:
            result.setdefault("warnings", []).extend(warnings)
        result["recovery_required"] = True
        result["journal_error"] = str(e)


def _finalize_failed_apply(journal, txn_id: int, result: dict, restored_ok: bool, warnings: list) -> None:
    """Records the outcome of a rollback on both the journal row and the
    result dict the caller returns, so the app/tests can see what happened
    without querying the journal."""
    info = {"apply_error": result.get("error")}
    if warnings:
        info["warnings"] = warnings
        result.setdefault("warnings", []).extend(warnings)
    if restored_ok:
        journal.mark_rolled_back(txn_id, info)
        result["rolled_back"] = True
    else:
        journal.mark_recovery_required(txn_id, info)
        result["recovery_required"] = True


def _rollback_music_apply(path: str, pre_apply_tags: dict, pre_apply_art, result: dict) -> "tuple[bool, list]":
    """Compensate a failed music apply, in place at `path` (rename is last,
    so a failure means the file never moved). Returns (fully_restored,
    warnings)."""
    warnings: list = []
    fully_restored = True

    if result.get("tagged"):
        try:
            tagger_module.set_or_clear_tags(path, **pre_apply_tags)
        except Exception as e:
            fully_restored = False
            warnings.append(f"Could not restore original tags on '{os.path.basename(path)}': {e}")

    if result.get("art_embedded"):
        try:
            if pre_apply_art is None:
                # No art before this apply (or it couldn't be snapshotted) -
                # strip what we embedded to get back to an art-free state.
                tagger_module.remove_cover_art(path)
            else:
                art_bytes, art_mime = pre_apply_art
                tagger_module.embed_cover_art(path, art_bytes, art_mime)
        except Exception as e:
            # Embedded art is exactly what undo also declines to fully
            # manage; a failure to revert it is cosmetic, not a structural
            # inconsistency, so it warns rather than forcing recovery.
            warnings.append(f"Could not revert embedded cover art on '{os.path.basename(path)}': {e}")

    return fully_restored, warnings


def _rollback_movie_apply(path: str, pre_apply: dict, result: dict) -> "tuple[bool, list]":
    """Compensate a failed movie apply, in place at `path`. Reverts mp4/m4v
    embedded atoms and created/overwritten .nfo/poster sidecars; an ffmpeg
    remux (mkv/avi/mov/wmv) can't be reversed without the original bytes, so
    it's surfaced as a warning. Returns (fully_restored, warnings)."""
    warnings: list = []
    fully_restored = True
    ext = os.path.splitext(path)[1].lower()

    if result.get("tagged"):
        if ext in (".mp4", ".m4v"):
            try:
                from mutagen.mp4 import MP4
                audio = MP4(path)
                if pre_apply.get("tag_title"):
                    audio["\xa9nam"] = [pre_apply["tag_title"]]
                elif "\xa9nam" in audio:
                    del audio["\xa9nam"]
                if pre_apply.get("tag_year"):
                    audio["\xa9day"] = [str(pre_apply["tag_year"])]
                elif "\xa9day" in audio:
                    del audio["\xa9day"]
                audio.save()
            except Exception as e:
                fully_restored = False
                warnings.append(f"Could not revert embedded metadata on '{os.path.basename(path)}': {e}")
        else:
            # The embed went through an ffmpeg remux, which rewrote the source
            # file in place and can't be reverted without the original bytes.
            # The file therefore still carries the applied metadata, so this is
            # NOT a clean rollback: flag it as not fully restored so the
            # transaction becomes RECOVERY_REQUIRED, keeping ROLLED_BACK honest
            # (it must mean "before-state restored").
            fully_restored = False
            warnings.append(
                "Embedded metadata was written via an ffmpeg remux, which can't be "
                "automatically reverted - the video still carries the applied metadata "
                "and needs a manual check."
            )

    for key, had_key, bytes_key, label in (
        ("nfo_path", "had_nfo", "nfo_bytes_b64", ".nfo"),
        ("poster_path", "had_poster", "poster_bytes_b64", "poster"),
    ):
        created = result.get(key)
        if not created or not os.path.exists(created):
            continue
        try:
            if not pre_apply.get(had_key):
                # We created this sidecar fresh - remove it.
                os.remove(created)
            else:
                # It existed before and we overwrote it - restore its bytes
                # if we snapshotted them, else leave it and say so.
                original = _unb64_or_none(pre_apply.get(bytes_key))
                if original is not None:
                    with open(created, "wb") as f:
                        f.write(original)
                else:
                    warnings.append(
                        f"Original {label} couldn't be snapshotted before apply; "
                        "left as MetaMatch wrote it."
                    )
        except OSError as e:
            fully_restored = False
            warnings.append(f"Could not restore {label} '{os.path.basename(created)}': {e}")

    return fully_restored, warnings


def _revert_series_artifacts(snapshot_artifacts: dict, fingerprints: dict | None = None) -> "tuple[bool, list]":
    """Revert a set of series-level sidecar writes (tvshow.nfo, series/season
    posters) to their pre-write state: delete the ones we created, restore the
    bytes of any we overwrote. Shared by the write-time rollback (on a failed
    write) and user-initiated undo. When `fingerprints` is given (undo), an
    artifact the user has changed since we wrote it is left alone and flagged,
    rather than clobbered. The small tvshow.nfo is always snapshotted so its
    restore is structural (failure -> not fully restored); posters may exceed
    the snapshot cap, in which case an overwrite we can't reverse is a warning
    only, matching how movie/episode art is treated."""
    warnings: list = []
    fully_restored = True

    for abspath, snap in snapshot_artifacts.items():
        try:
            exists_now = os.path.exists(abspath)
            if fingerprints is not None and abspath in fingerprints:
                fp = fingerprints[abspath]
                if _fingerprint_changed(abspath, fp.get("size"), fp.get("mtime_ns"), fp.get("hash")):
                    warnings.append(f"Left '{os.path.basename(abspath)}' alone - it changed since MetaMatch wrote it.")
                    continue

            if not snap.get("had"):
                if exists_now:
                    os.remove(abspath)
            else:
                original = _unb64_or_none(snap.get("bytes_b64"))
                if original is not None:
                    with open(abspath, "wb") as f:
                        f.write(original)
                elif abspath.lower().endswith(".nfo"):
                    fully_restored = False
                    warnings.append(f"Couldn't restore original '{os.path.basename(abspath)}' (not snapshotted).")
                else:
                    warnings.append(f"Couldn't restore original '{os.path.basename(abspath)}' (too large to snapshot); left as written.")
        except OSError as e:
            fully_restored = False
            warnings.append(f"Could not revert '{os.path.basename(abspath)}': {e}")

    return fully_restored, warnings


_TV_MP4_ATOMS = ("\xa9nam", "\xa9day", "\xa9ART", "tvsh", "tvsn", "tves", "stik")


def _restore_mp4_atoms(path: str, atom_snapshot: "dict | None") -> None:
    """Restore every TV MP4 atom to its snapshotted prior state: set atoms
    that existed back to their exact value, and delete only those that didn't
    exist before the apply. This replaces the old "delete the whole set"
    behaviour, which destroyed pre-existing show/season/episode atoms on undo.
    Raises on MP4 I/O failure so callers can flag the rollback as incomplete."""
    from mutagen.mp4 import MP4
    audio = MP4(path)
    for atom in _TV_MP4_ATOMS:
        prior = atom_snapshot.get(atom)
        if prior is None:
            if atom in audio:
                del audio[atom]
        else:
            audio[atom] = prior
    audio.save()


def _rollback_tv_apply(path: str, pre_apply: dict, result: dict) -> "tuple[bool, list]":
    """Compensate a failed episode apply, in place at `path`. Mirrors
    _rollback_movie_apply: reverts mp4/m4v atoms and created/overwritten
    .nfo/-thumb.jpg sidecars; an ffmpeg remux can't be reversed without the
    original bytes, so it's surfaced as a warning. Returns (fully_restored,
    warnings). The episode embed writes several atoms (show/season/episode/
    stik) beyond title/year, so the revert restores that whole set to its
    snapshotted prior state rather than deleting it."""
    warnings: list = []
    fully_restored = True
    ext = os.path.splitext(path)[1].lower()

    if result.get("tagged"):
        if ext in (".mp4", ".m4v"):
            try:
                atom_snapshot = pre_apply.get("mp4_atoms")
                if atom_snapshot is not None:
                    _restore_mp4_atoms(path, atom_snapshot)
                else:
                    # Legacy row without a full atom snapshot: restore title/
                    # year only and leave TV-specific atoms untouched - never
                    # delete atoms we can't prove we created (see the matching
                    # fail-closed branch in the TV undo path).
                    from mutagen.mp4 import MP4
                    audio = MP4(path)
                    if pre_apply.get("tag_title"):
                        audio["\xa9nam"] = [pre_apply["tag_title"]]
                    elif "\xa9nam" in audio:
                        del audio["\xa9nam"]
                    if pre_apply.get("tag_year"):
                        audio["\xa9day"] = [str(pre_apply["tag_year"])]
                    elif "\xa9day" in audio:
                        del audio["\xa9day"]
                    audio.save()
                    warnings.append(
                        "Original show/season/episode atoms weren't recorded for this file, so "
                        "they were left as-is rather than deleted during rollback.")
            except Exception as e:
                fully_restored = False
                warnings.append(f"Could not revert embedded metadata on '{os.path.basename(path)}': {e}")
        else:
            # See _rollback_movie_apply: a remux can't be reverted, so the
            # file still carries the applied metadata - not a clean rollback.
            fully_restored = False
            warnings.append(
                "Embedded metadata was written via an ffmpeg remux, which can't be "
                "automatically reverted - the video still carries the applied metadata "
                "and needs a manual check."
            )

    for key, had_key, bytes_key, label in (
        ("nfo_path", "had_nfo", "nfo_bytes_b64", ".nfo"),
        ("thumb_path", "had_thumb", "thumb_bytes_b64", "thumbnail"),
    ):
        created = result.get(key)
        if not created or not os.path.exists(created):
            continue
        try:
            if not pre_apply.get(had_key):
                os.remove(created)
            else:
                original = _unb64_or_none(pre_apply.get(bytes_key))
                if original is not None:
                    with open(created, "wb") as f:
                        f.write(original)
                else:
                    warnings.append(
                        f"Original {label} couldn't be snapshotted before apply; "
                        "left as MetaMatch wrote it."
                    )
        except OSError as e:
            fully_restored = False
            warnings.append(f"Could not restore {label} '{os.path.basename(created)}': {e}")

    return fully_restored, warnings


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
        self._mutation_locks = _PathLockRegistry()

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

    def get_outstanding_recovery(self) -> list[dict]:
        """Every transaction still sitting at RECOVERY_REQUIRED in the
        journal - a rollback that couldn't fully restore, or a crash caught
        mid-apply. Unlike get_recovery_notices() (only this startup's
        findings), these persist across restarts until resolved, so the UI
        can keep flagging a file that genuinely needs a human until someone
        deals with it."""
        return [t.to_dict() for t in self.journal.list_by_status("music", journal_module.RECOVERY_REQUIRED)]
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
        with self._mutation_locks.get(track.path):
            return self._apply_one(track, do_tag, do_rename, do_art)

    def apply_all(self, do_tag: bool = True, do_rename: bool = True, do_art: bool = False,
                  min_confidence: float = 75.0, min_margin: float = 0.0) -> dict:
        """Applies every scanned track whose match confidence is at or above min_confidence."""
        min_confidence = _validate_confidence(min_confidence)
        min_margin = _validate_margin(min_margin)
        with self._lock:
            candidates = [self.tracks[p] for p in self.order]

        results = []
        for track in candidates:
            if not _passes_apply_thresholds(track.match, min_confidence, min_margin):
                continue
            with self._mutation_locks.get(track.path):
                results.append(self._apply_one(track, do_tag, do_rename, do_art))

        succeeded = sum(1 for r in results if not r["error"])
        return {
            "attempted": len(results), "succeeded": succeeded, "failed": len(results) - succeeded,
            "results": results,
        }

    def _apply_one(self, track: scanner_module.TrackFile, do_tag: bool, do_rename: bool, do_art: bool) -> dict:
        safe, reason = pathsafe_module.validate_mutation_target(track.path, self.folder)
        if not safe:
            return {
                "original_path": track.path, "new_path": track.path,
                "tagged": False, "renamed": False, "art_embedded": False,
                "error": f"Refused to modify '{os.path.basename(track.path)}': {reason}.",
            }
        if _fingerprint_changed(track.path, track.size_bytes, track.mtime_ns, track.content_hash):
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

        # This apply's own rollback target is the file's state RIGHT NOW,
        # not the chained true-original 'before_state' above. If this file
        # was already applied once, rolling back a failed *second* apply
        # must land on the committed first-apply state - which is what these
        # capture - rather than reverting the whole lineage (undo's job).
        pre_apply_tags = self._snapshot_original_tags(track)
        pre_apply_art = tagger_module.read_cover_art(track.path) if do_art else None

        operation = {"do_tag": do_tag, "do_rename": do_rename, "do_art": do_art}
        _base = {"original_path": track.path, "new_path": track.path,
                 "tagged": False, "renamed": False, "art_embedded": False, "error": None}
        try:
            txn_id = self.journal.begin("music", true_original_path, track.path, before_state, operation, library_root=self.folder)
        except Exception as e:
            return _journal_unavailable_result(_base, e)

        art_bytes = art_mime = None
        if do_art and track.match and track.match.get("release_id"):
            fetched = art_module.fetch_cover_art(track.match["release_id"], size="500")
            if fetched:
                art_bytes, art_mime = fetched

        # Past this point files are actually mutated - mark 'applying' so a
        # crash mid-mutation is recoverable as such (vs a benign 'pending').
        try:
            self.journal.mark_applying(txn_id)
        except Exception as e:
            return _journal_unavailable_result(_base, e)
        result = tagger_module.apply_match(
            track.path, track.match, do_tag=do_tag, do_rename=do_rename,
            do_art=do_art, art_bytes=art_bytes, art_mime=art_mime,
        )
        result["txn_id"] = txn_id
        result["rolled_back"] = False
        result["recovery_required"] = False

        if result["error"]:
            _mark_rolling_back_safely(self.journal, txn_id)
            restored_ok, warnings = _rollback_music_apply(track.path, pre_apply_tags, pre_apply_art, result)
            _finalize_failed_apply_safely(self.journal, txn_id, result, restored_ok, warnings)
        else:
            after_state = None
            media_fp = _file_fingerprint(result["new_path"])
            if media_fp:
                after_state = {
                    "media_size": media_fp["size"], "media_mtime_ns": media_fp["mtime_ns"],
                    "media_hash": media_fp["hash"],
                }
            committed = _safe_commit(self.journal, txn_id, result["new_path"], result, after_state=after_state)
            if committed and existing:
                try:
                    self.journal.mark_superseded(existing.id)
                except Exception:
                    pass

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
        with self._mutation_locks.get(txn.current_path):
            return self._undo_txn(txn)

    def undo_all(self) -> dict:
        if not self.folder:
            raise ValueError("Scan or select a library before using Undo All. "
                             "(Individual files can still be undone by name after a restart.)")
        txns = self.journal.list_undoable("music", folder=self.folder)
        results = []
        for t in txns:
            with self._mutation_locks.get(t.current_path):
                results.append(self._undo_txn(t))
        succeeded = sum(1 for r in results if not r["error"])
        return {"restored": succeeded, "results": results}

    def _undo_txn(self, txn: journal_module.Transaction) -> dict:
        current_path = txn.current_path
        original_path = txn.original_path
        result = {"restored_path": current_path, "error": None}

        # On a restart, self.folder is None (no active scan) but the journal
        # recorded which library root authorised this operation - use it so
        # Undo re-applies the SAME authority validation (link/reparse,
        # containment AND hard-link) it had at Apply time, rather than the
        # weaker no-root fallback.
        auth_root = self.folder or txn.library_root
        auth = _authority_error(auth_root, current_path)
        if auth:
            result["error"] = f"Refused to undo '{os.path.basename(current_path)}': {auth}."
            return result

        after_state = txn.after_state or {}
        expected_size = after_state.get("media_size")
        expected_mtime_ns = after_state.get("media_mtime_ns")
        if _fingerprint_changed(current_path, expected_size, expected_mtime_ns, after_state.get("media_hash")):
            result["error"] = (
                "This file has changed since MetaMatch applied this match "
                "(size/modification time no longer match) - undo refused to "
                "avoid modifying a different file. Rescan if you still want to change it."
            )
            return result

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
            fingerprints = {p: (self.tracks[p].size_bytes, self.tracks[p].mtime_ns, self.tracks[p].content_hash) for p in valid_paths}

        if not folder:
            raise ValueError("Scan a folder first.")
        if not paths:
            raise ValueError("No files selected.")

        rejected = [p for p in paths if p not in valid_paths]
        results = []

        # Hold each target file's mutation lock across its stale-check and
        # move, so a concurrent Apply/Undo on the same file can't interleave
        # with quarantining it (which would race the file out from under the
        # other operation). Released before the in-memory cleanup below.
        with _locked_paths(self._mutation_locks, valid_paths):
            # A file that's tracked but no longer matches what was recorded at
            # scan time (replaced/modified since) is refused rather than moved -
            # the same TOCTOU guard apply() already uses, applied here too so
            # quarantine can't be tricked into moving unrelated content that
            # happens to now sit at a previously-scanned path.
            still_valid = []
            for p in valid_paths:
                auth = _authority_error(folder, p)
                if auth:
                    results.append({
                        "original_path": p, "new_path": None,
                        "error": f"Refused to quarantine '{os.path.basename(p)}': {auth}.",
                    })
                    continue
                size, mtime_ns, content_hash = fingerprints[p]
                if _fingerprint_changed(p, size, mtime_ns, content_hash):
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
        self._mutation_locks = _PathLockRegistry()

        self.journal = journal or journal_module.Journal(journal_module.DEFAULT_JOURNAL_PATH)
        self.recovered_transactions = self.journal.recover("movie")
        self.swept_orphan_temps: list[str] = []

    def get_recovery_notices(self) -> list[dict]:
        return [t.to_dict() for t in self.recovered_transactions]

    def get_outstanding_recovery(self) -> list[dict]:
        """Movie transactions still at RECOVERY_REQUIRED - persists across
        restarts until resolved (see MusicLibrary.get_outstanding_recovery)."""
        return [t.to_dict() for t in self.journal.list_by_status("movie", journal_module.RECOVERY_REQUIRED)]

    @property
    def ffprobe_available(self) -> bool:
        return video_scanner_module.FFPROBE_AVAILABLE

    @property
    def ffmpeg_available(self) -> bool:
        return movie_tagger_module.FFMPEG_AVAILABLE

    # ---------------------------------------------------------------- scan
    def scan(self, folder: str, recursive: bool = True) -> list[dict]:
        videos = video_scanner_module.scan_folder(folder, recursive=recursive)
        # Clean up any remux temp files a killed process orphaned here (age-
        # guarded, so an in-progress remux is never disturbed). Best-effort:
        # recorded for surfacing but never allowed to fail a scan.
        self.swept_orphan_temps = movie_tagger_module.sweep_orphan_remux_temps(folder)
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
        with self._mutation_locks.get(video.path):
            return self._apply_one(video, do_tag, do_rename, do_nfo, do_poster)

    def apply_all(self, do_tag: bool = False, do_rename: bool = True, do_nfo: bool = True,
                  do_poster: bool = True, min_confidence: float = 75.0, min_margin: float = 0.0) -> dict:
        min_confidence = _validate_confidence(min_confidence)
        min_margin = _validate_margin(min_margin)
        with self._lock:
            candidates = [self.videos[p] for p in self.order]

        results = []
        for video in candidates:
            if not _passes_apply_thresholds(video.match, min_confidence, min_margin):
                continue
            with self._mutation_locks.get(video.path):
                results.append(self._apply_one(video, do_tag, do_rename, do_nfo, do_poster))

        succeeded = sum(1 for r in results if not r["error"])
        return {
            "attempted": len(results), "succeeded": succeeded, "failed": len(results) - succeeded,
            "results": results,
        }

    def _apply_one(self, video: video_scanner_module.VideoFile, do_tag: bool, do_rename: bool,
                    do_nfo: bool, do_poster: bool) -> dict:
        safe, reason = pathsafe_module.validate_mutation_target(video.path, self.folder)
        if not safe:
            return {
                "original_path": video.path, "new_path": video.path,
                "tagged": False, "renamed": False, "nfo_path": None, "poster_path": None,
                "error": f"Refused to modify '{os.path.basename(video.path)}': {reason}.",
            }
        if _fingerprint_changed(video.path, video.size_bytes, video.mtime_ns, video.content_hash):
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

        # Immediate pre-apply state = this apply's rollback target (see the
        # music path for why this is captured separately from 'snapshot',
        # which is undo's whole-lineage target).
        pre_apply = self._snapshot_original(video)

        operation = {"do_tag": do_tag, "do_rename": do_rename, "do_nfo": do_nfo, "do_poster": do_poster}
        _base = {"original_path": video.path, "new_path": video.path,
                 "tagged": False, "renamed": False, "nfo_path": None, "poster_path": None, "error": None}
        try:
            txn_id = self.journal.begin("movie", true_original_path, video.path, snapshot, operation, library_root=self.folder)
        except Exception as e:
            return _journal_unavailable_result(_base, e)

        try:
            self.journal.mark_applying(txn_id)
        except Exception as e:
            return _journal_unavailable_result(_base, e)
        result = movie_tagger_module.apply_movie_match(
            video.path, video.match, do_tag=do_tag, do_rename=do_rename, do_nfo=do_nfo, do_poster=do_poster,
        )
        result["txn_id"] = txn_id
        result["rolled_back"] = False
        result["recovery_required"] = False

        if result["error"]:
            _mark_rolling_back_safely(self.journal, txn_id)
            restored_ok, warnings = _rollback_movie_apply(video.path, pre_apply, result)
            _finalize_failed_apply_safely(self.journal, txn_id, result, restored_ok, warnings)
        else:
            # Record the EXACT sidecar paths this apply actually produced,
            # not a path undo could reconstruct later - a rename-time name
            # collision can push a sidecar to an alternate suffixed name
            # (see movie_tagger._safe_move), and undo must operate on that
            # real path or risk deleting an unrelated file that happens to
            # sit at the naively-expected name instead. Fingerprints of
            # each object as apply left them let undo verify later that
            # nothing else has touched them since.
            after_state = {"nfo_path": result.get("nfo_path"), "poster_path": result.get("poster_path")}
            media_fp = _file_fingerprint(result["new_path"])
            if media_fp:
                after_state["media_size"] = media_fp["size"]
                after_state["media_mtime_ns"] = media_fp["mtime_ns"]
                after_state["media_hash"] = media_fp["hash"]
            nfo_fp = _file_fingerprint(result.get("nfo_path"))
            if nfo_fp:
                after_state["nfo_size"] = nfo_fp["size"]
                after_state["nfo_mtime_ns"] = nfo_fp["mtime_ns"]
                after_state["nfo_hash"] = nfo_fp["hash"]
            poster_fp = _file_fingerprint(result.get("poster_path"))
            if poster_fp:
                after_state["poster_size"] = poster_fp["size"]
                after_state["poster_mtime_ns"] = poster_fp["mtime_ns"]
                after_state["poster_hash"] = poster_fp["hash"]

            committed = _safe_commit(self.journal, txn_id, result["new_path"], result, after_state=after_state)
            if committed and existing:
                try:
                    self.journal.mark_superseded(existing.id)
                except Exception:
                    pass

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
        with self._mutation_locks.get(txn.current_path):
            return self._undo_txn(txn)

    def undo_all(self) -> dict:
        if not self.folder:
            raise ValueError("Scan or select a library before using Undo All. "
                             "(Individual files can still be undone by name after a restart.)")
        txns = self.journal.list_undoable("movie", folder=self.folder)
        results = []
        for t in txns:
            with self._mutation_locks.get(t.current_path):
                results.append(self._undo_txn(t))
        succeeded = sum(1 for r in results if not r["error"])
        return {"restored": succeeded, "results": results}

    def _undo_txn(self, txn: journal_module.Transaction) -> dict:
        current_path = txn.current_path
        original_path = txn.original_path
        snapshot = txn.before_state
        after_state = txn.after_state or {}
        result = {"restored_path": current_path, "error": None, "warnings": []}

        # On a restart, self.folder is None (no active scan) but the journal
        # recorded which library root authorised this operation - use it so
        # Undo re-applies the SAME authority validation (link/reparse,
        # containment AND hard-link) it had at Apply time, rather than the
        # weaker no-root fallback.
        auth_root = self.folder or txn.library_root
        auth = _authority_error(auth_root, current_path)
        if auth:
            result["error"] = f"Refused to undo '{os.path.basename(current_path)}': {auth}."
            return result

        # The video itself: refuse the whole undo if it's changed since
        # apply produced it - same principle as apply()'s own TOCTOU guard,
        # applied to undo. No fingerprint recorded (a transaction from
        # before this check existed) means nothing to verify against, so
        # it's allowed through rather than blocking all older undo history.
        if _fingerprint_changed(current_path, after_state.get("media_size"), after_state.get("media_mtime_ns"), after_state.get("media_hash")):
            result["error"] = (
                "This file has changed since MetaMatch applied this match "
                "(size/modification time no longer match) - undo refused to "
                "avoid modifying a different file. Rescan if you still want to change it."
            )
            return result

        try:
            # Use the EXACT sidecar paths recorded when this apply ran
            # (see _apply_one) rather than reconstructing them from the
            # current video filename - a rename-time collision with an
            # unrelated file can push a sidecar to an alternate suffixed
            # name, and guessing at "current basename + .nfo" can land on
            # that unrelated file instead of the one MetaMatch actually
            # created, deleting or overwriting something it never touched.
            has_exact_paths = "nfo_path" in after_state or "poster_path" in after_state
            if has_exact_paths:
                nfo_current = after_state.get("nfo_path")
                poster_current = after_state.get("poster_path")
            else:
                # Transaction predates exact-path tracking (a journal from
                # before this fix) - there's no way to recover the real
                # sidecar path for it, and guessing risks the exact
                # unrelated-file deletion this feature exists to prevent.
                # Fail closed: skip sidecar undo entirely for these rather
                # than guess. The video filename/tags are still restored.
                nfo_current = None
                poster_current = None
                result["warnings"].append(
                    "This transaction predates exact sidecar tracking, so its .nfo/poster "
                    "(if any) were left untouched - check them manually if needed."
                )

            # Sidecars this apply actually produced get an extra check: if
            # one changed since apply left it, skip touching that specific
            # sidecar (but still proceed with the rest of undo) rather than
            # delete or overwrite something that isn't what MetaMatch wrote.
            if nfo_current and _fingerprint_changed(nfo_current, after_state.get("nfo_size"), after_state.get("nfo_mtime_ns"), after_state.get("nfo_hash")):
                result["warnings"].append(
                    f"Skipped restoring '{os.path.basename(nfo_current)}' - it has changed since apply."
                )
                nfo_current = None
            if poster_current and _fingerprint_changed(poster_current, after_state.get("poster_size"), after_state.get("poster_mtime_ns"), after_state.get("poster_hash")):
                result["warnings"].append(
                    f"Skipped restoring '{os.path.basename(poster_current)}' - it has changed since apply."
                )
                poster_current = None

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
            # was False, a fingerprint mismatch above, or a legacy
            # transaction with no known sidecar path) is skipped entirely.
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
            fingerprints = {p: (self.videos[p].size_bytes, self.videos[p].mtime_ns, self.videos[p].content_hash) for p in valid_paths}

        if not folder:
            raise ValueError("Scan a folder first.")
        if not paths:
            raise ValueError("No files selected.")

        rejected = [p for p in paths if p not in valid_paths]
        results = []

        # Hold each target file's mutation lock across its stale-check and move
        # (see MusicLibrary.quarantine for why).
        with _locked_paths(self._mutation_locks, valid_paths):
            # Same TOCTOU guard apply() uses: refuse a file that no longer
            # matches what was recorded at scan time, rather than quarantining
            # whatever now happens to sit at that path.
            still_valid = []
            for p in valid_paths:
                auth = _authority_error(folder, p)
                if auth:
                    results.append({
                        "original_path": p, "new_path": None,
                        "error": f"Refused to quarantine '{os.path.basename(p)}': {auth}.",
                    })
                    continue
                size, mtime_ns, content_hash = fingerprints[p]
                if _fingerprint_changed(p, size, mtime_ns, content_hash):
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


# =====================================================================
# TvLibrary - the episode analogue of MovieLibrary. Same journal-backed
# apply/undo/rollback/recovery machinery (journal kind "tv"), same
# fail-closed guarantees, adapted to the series/season/episode model and
# the .nfo/-thumb.jpg episode sidecars. See MovieLibrary for the shared
# rationale behind each step; only the TV-specific differences are
# commented here.
# =====================================================================

class TvLibrary:
    """One scanned folder of TV episode files, its TMDB episode matches, and
    journal-backed undo history (see journal.py)."""

    def __init__(self, journal: Optional[journal_module.Journal] = None):
        self.folder: Optional[str] = None
        self.episodes: dict[str, episode_scanner_module.EpisodeFile] = {}
        self.order: list[str] = []
        self.match_progress: dict = {"running": False, "done": 0, "total": 0, "error": None}
        self._lock = threading.RLock()
        self._mutation_locks = _PathLockRegistry()

        self.journal = journal or journal_module.Journal(journal_module.DEFAULT_JOURNAL_PATH)
        # Episode applies use kind "tv"; series-level artifacts (tvshow.nfo,
        # series/season posters) use kind "tv_series" so they never show up
        # in per-episode undo but still get full crash recovery.
        self.recovered_transactions = (
            self.journal.recover("tv") + self.journal.recover("tv_series")
        )
        self.swept_orphan_temps: list[str] = []

    def get_recovery_notices(self) -> list[dict]:
        return [t.to_dict() for t in self.recovered_transactions]

    def get_outstanding_recovery(self) -> list[dict]:
        """Episode and series transactions still at RECOVERY_REQUIRED -
        persists across restarts until resolved (see
        MusicLibrary.get_outstanding_recovery)."""
        return [
            t.to_dict() for t in (
                self.journal.list_by_status("tv", journal_module.RECOVERY_REQUIRED) +
                self.journal.list_by_status("tv_series", journal_module.RECOVERY_REQUIRED)
            )
        ]

    @property
    def ffprobe_available(self) -> bool:
        return video_scanner_module.FFPROBE_AVAILABLE

    @property
    def ffmpeg_available(self) -> bool:
        return movie_tagger_module.FFMPEG_AVAILABLE

    # ---------------------------------------------------------------- scan
    def scan(self, folder: str, recursive: bool = True) -> list[dict]:
        episodes = episode_scanner_module.scan_folder(folder, recursive=recursive)
        self.swept_orphan_temps = movie_tagger_module.sweep_orphan_remux_temps(folder)
        with self._lock:
            self.folder = folder
            self.episodes = {e.path: e for e in episodes}
            self.order = [e.path for e in episodes]
            self.match_progress = {"running": False, "done": 0, "total": len(episodes), "error": None}
        return self.episodes_payload()

    def episodes_payload(self) -> list[dict]:
        with self._lock:
            undoable = self.journal.get_undoable_paths("tv", folder=self.folder)
            out = []
            for p in self.order:
                d = self.episodes[p].to_dict()
                d["can_undo"] = p in undoable
                out.append(d)
            return out

    # ------------------------------------------------------------- match
    def match(self, progress_callback: ProgressCallback = None) -> None:
        with self._lock:
            episodes = [self.episodes[p] for p in self.order]
            self.match_progress = {"running": True, "done": 0, "total": len(episodes), "error": None}

        def on_progress(done, total):
            with self._lock:
                self.match_progress["done"] = done
                self.match_progress["total"] = total
            if progress_callback:
                progress_callback(done, total)

        error_message = None
        try:
            tv_matcher_module.match_episodes(episodes, progress_callback=on_progress)
        except movie_matcher_module.TmdbNotConfigured as e:
            error_message = str(e)
        except Exception as e:
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
    def _read_original_atoms(self, path: str) -> tuple:
        """The pre-apply title/year embedded in an mp4/m4v (kept for backward
        compatibility with older journal rows). Non-mp4 containers return
        (None, None) - their embed goes through a remux that rollback can't
        reverse anyway."""
        if os.path.splitext(path)[1].lower() not in (".mp4", ".m4v"):
            return (None, None)
        try:
            from mutagen.mp4 import MP4
            audio = MP4(path)
            title = (audio.get("\xa9nam") or [None])[0]
            day = (audio.get("\xa9day") or [None])[0]
            year = str(day)[:4] if day else None
            return (title, year)
        except Exception:
            return (None, None)

    def _read_original_mp4_atoms(self, path: str) -> "dict | None":
        """Snapshot every MP4 atom a TV apply might overwrite, so undo/rollback
        can restore each to its EXACT prior value instead of blindly deleting
        it. An episode apply writes show/season/episode/media-kind/artist atoms
        on top of title/year - a file that was already tagged (a re-tag, or a
        different tagger's output) has real values in those atoms, and deleting
        them on undo is data loss. Each atom maps to its prior list value, or
        None if it wasn't present. Returns None for non-mp4 containers (the
        remux path can't be reverted atom-by-atom anyway)."""
        if os.path.splitext(path)[1].lower() not in (".mp4", ".m4v"):
            return None
        try:
            from mutagen.mp4 import MP4
            audio = MP4(path)
            snap = {}
            for atom in _TV_MP4_ATOMS:
                snap[atom] = list(audio[atom]) if atom in audio else None
            return snap
        except Exception:
            return None

    def _snapshot_original(self, episode: episode_scanner_module.EpisodeFile) -> dict:
        base = os.path.splitext(episode.path)[0]
        nfo_path = base + ".nfo"
        thumb_path = base + tv_tagger_module.THUMB_SUFFIX

        nfo_bytes = _read_small_file(nfo_path, _MAX_SIDECAR_SNAPSHOT_BYTES)
        thumb_bytes = _read_small_file(thumb_path, _MAX_SIDECAR_SNAPSHOT_BYTES)
        tag_title, tag_year = self._read_original_atoms(episode.path)

        return {
            # tag_title/tag_year kept for older-journal-row compatibility;
            # mp4_atoms is the authoritative full snapshot used by rollback/undo.
            "tag_title": tag_title,
            "tag_year": tag_year,
            "mp4_atoms": self._read_original_mp4_atoms(episode.path),
            "had_nfo": os.path.exists(nfo_path),
            "nfo_bytes_b64": _b64_or_none(nfo_bytes),
            "had_thumb": os.path.exists(thumb_path),
            "thumb_bytes_b64": _b64_or_none(thumb_bytes),
        }

    def apply(self, episode_id: str, do_tag: bool = False, do_rename: bool = True,
              do_nfo: bool = True, do_thumb: bool = True) -> dict:
        with self._lock:
            episode = self.episodes.get(episode_id)
        if not episode:
            raise KeyError(f"Unknown file: {episode_id}")
        if not episode.match:
            raise ValueError("This file has no match to apply.")
        with self._mutation_locks.get(episode.path):
            return self._apply_one(episode, do_tag, do_rename, do_nfo, do_thumb)

    def apply_all(self, do_tag: bool = False, do_rename: bool = True, do_nfo: bool = True,
                  do_thumb: bool = True, min_confidence: float = 75.0, min_margin: float = 0.0) -> dict:
        min_confidence = _validate_confidence(min_confidence)
        min_margin = _validate_margin(min_margin)
        with self._lock:
            candidates = [self.episodes[p] for p in self.order]

        results = []
        for episode in candidates:
            if not _passes_apply_thresholds(episode.match, min_confidence, min_margin):
                continue
            with self._mutation_locks.get(episode.path):
                results.append(self._apply_one(episode, do_tag, do_rename, do_nfo, do_thumb))

        succeeded = sum(1 for r in results if not r["error"])
        return {
            "attempted": len(results), "succeeded": succeeded, "failed": len(results) - succeeded,
            "results": results,
        }

    def _apply_one(self, episode: episode_scanner_module.EpisodeFile, do_tag: bool, do_rename: bool,
                   do_nfo: bool, do_thumb: bool) -> dict:
        safe, reason = pathsafe_module.validate_mutation_target(episode.path, self.folder)
        if not safe:
            return {
                "original_path": episode.path, "new_path": episode.path,
                "tagged": False, "renamed": False, "nfo_path": None, "thumb_path": None,
                "error": f"Refused to modify '{os.path.basename(episode.path)}': {reason}.",
            }
        if _fingerprint_changed(episode.path, episode.size_bytes, episode.mtime_ns, episode.content_hash):
            return {
                "original_path": episode.path, "new_path": episode.path,
                "tagged": False, "renamed": False, "nfo_path": None, "thumb_path": None,
                "error": "File changed on disk since it was scanned/matched - rescan before applying.",
            }

        existing = self.journal.get_active_for_path("tv", episode.path)
        if existing:
            true_original_path = existing.original_path
            snapshot = existing.before_state
        else:
            true_original_path = episode.path
            snapshot = self._snapshot_original(episode)

        pre_apply = self._snapshot_original(episode)

        operation = {"do_tag": do_tag, "do_rename": do_rename, "do_nfo": do_nfo, "do_thumb": do_thumb}
        _base = {"original_path": episode.path, "new_path": episode.path,
                 "tagged": False, "renamed": False, "nfo_path": None, "thumb_path": None, "error": None}
        try:
            txn_id = self.journal.begin("tv", true_original_path, episode.path, snapshot, operation, library_root=self.folder)
        except Exception as e:
            return _journal_unavailable_result(_base, e)

        try:
            self.journal.mark_applying(txn_id)
        except Exception as e:
            return _journal_unavailable_result(_base, e)
        result = tv_tagger_module.apply_episode_match(
            episode.path, episode.match, do_tag=do_tag, do_rename=do_rename, do_nfo=do_nfo, do_thumb=do_thumb,
        )
        result["txn_id"] = txn_id
        result["rolled_back"] = False
        result["recovery_required"] = False

        if result["error"]:
            _mark_rolling_back_safely(self.journal, txn_id)
            restored_ok, warnings = _rollback_tv_apply(episode.path, pre_apply, result)
            _finalize_failed_apply_safely(self.journal, txn_id, result, restored_ok, warnings)
        else:
            after_state = {"nfo_path": result.get("nfo_path"), "thumb_path": result.get("thumb_path")}
            media_fp = _file_fingerprint(result["new_path"])
            if media_fp:
                after_state["media_size"] = media_fp["size"]
                after_state["media_mtime_ns"] = media_fp["mtime_ns"]
                after_state["media_hash"] = media_fp["hash"]
            nfo_fp = _file_fingerprint(result.get("nfo_path"))
            if nfo_fp:
                after_state["nfo_size"] = nfo_fp["size"]
                after_state["nfo_mtime_ns"] = nfo_fp["mtime_ns"]
                after_state["nfo_hash"] = nfo_fp["hash"]
            thumb_fp = _file_fingerprint(result.get("thumb_path"))
            if thumb_fp:
                after_state["thumb_size"] = thumb_fp["size"]
                after_state["thumb_mtime_ns"] = thumb_fp["mtime_ns"]
                after_state["thumb_hash"] = thumb_fp["hash"]

            committed = _safe_commit(self.journal, txn_id, result["new_path"], result, after_state=after_state)
            if committed and existing:
                try:
                    self.journal.mark_superseded(existing.id)
                except Exception:
                    pass

            with self._lock:
                if episode.path in self.episodes:
                    del self.episodes[episode.path]
                refreshed = episode_scanner_module.read_episode(result["new_path"])
                refreshed.match = episode.match
                self.episodes[refreshed.path] = refreshed
                if episode.path in self.order:
                    idx = self.order.index(episode.path)
                    self.order[idx] = refreshed.path

        return result

    # ------------------------------------------------------------ undo
    def undo(self, episode_id: str) -> dict:
        txn = self.journal.get_active_for_path("tv", episode_id)
        if not txn:
            raise ValueError("Nothing to undo for this file.")
        with self._mutation_locks.get(txn.current_path):
            return self._undo_txn(txn)

    def undo_all(self) -> dict:
        if not self.folder:
            raise ValueError("Scan or select a library before using Undo All. "
                             "(Individual files can still be undone by name after a restart.)")
        txns = self.journal.list_undoable("tv", folder=self.folder)
        results = []
        for t in txns:
            with self._mutation_locks.get(t.current_path):
                results.append(self._undo_txn(t))
        succeeded = sum(1 for r in results if not r["error"])
        return {"restored": succeeded, "results": results}

    def _undo_txn(self, txn: journal_module.Transaction) -> dict:
        current_path = txn.current_path
        original_path = txn.original_path
        snapshot = txn.before_state
        after_state = txn.after_state or {}
        result = {"restored_path": current_path, "error": None, "warnings": []}

        # On a restart, self.folder is None (no active scan) but the journal
        # recorded which library root authorised this operation - use it so
        # Undo re-applies the SAME authority validation (link/reparse,
        # containment AND hard-link) it had at Apply time, rather than the
        # weaker no-root fallback.
        auth_root = self.folder or txn.library_root
        auth = _authority_error(auth_root, current_path)
        if auth:
            result["error"] = f"Refused to undo '{os.path.basename(current_path)}': {auth}."
            return result

        if _fingerprint_changed(current_path, after_state.get("media_size"), after_state.get("media_mtime_ns"), after_state.get("media_hash")):
            result["error"] = (
                "This file has changed since MetaMatch applied this match "
                "(size/modification time no longer match) - undo refused to "
                "avoid modifying a different file. Rescan if you still want to change it."
            )
            return result

        try:
            has_exact_paths = "nfo_path" in after_state or "thumb_path" in after_state
            if has_exact_paths:
                nfo_current = after_state.get("nfo_path")
                thumb_current = after_state.get("thumb_path")
            else:
                nfo_current = None
                thumb_current = None
                result["warnings"].append(
                    "This transaction predates exact sidecar tracking, so its .nfo/thumbnail "
                    "(if any) were left untouched - check them manually if needed."
                )

            if nfo_current and _fingerprint_changed(nfo_current, after_state.get("nfo_size"), after_state.get("nfo_mtime_ns"), after_state.get("nfo_hash")):
                result["warnings"].append(
                    f"Skipped restoring '{os.path.basename(nfo_current)}' - it has changed since apply."
                )
                nfo_current = None
            if thumb_current and _fingerprint_changed(thumb_current, after_state.get("thumb_size"), after_state.get("thumb_mtime_ns"), after_state.get("thumb_hash")):
                result["warnings"].append(
                    f"Skipped restoring '{os.path.basename(thumb_current)}' - it has changed since apply."
                )
                thumb_current = None

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
                if thumb_current and os.path.exists(thumb_current):
                    thumb_current = movie_tagger_module._safe_move(thumb_current, base_original + tv_tagger_module.THUMB_SUFFIX)

            nfo_bytes = _unb64_or_none(snapshot.get("nfo_bytes_b64"))
            thumb_bytes = _unb64_or_none(snapshot.get("thumb_bytes_b64"))

            if nfo_current:
                if not snapshot.get("had_nfo"):
                    if os.path.exists(nfo_current):
                        os.remove(nfo_current)
                elif nfo_bytes is not None:
                    with open(nfo_current, "wb") as f:
                        f.write(nfo_bytes)

            if thumb_current:
                if not snapshot.get("had_thumb"):
                    if os.path.exists(thumb_current):
                        os.remove(thumb_current)
                elif thumb_bytes is not None:
                    with open(thumb_current, "wb") as f:
                        f.write(thumb_bytes)

            ext = os.path.splitext(path_to_use)[1].lower()
            if ext in (".mp4", ".m4v"):
                atom_snapshot = snapshot.get("mp4_atoms")
                if atom_snapshot is not None:
                    # Restore every atom to its exact prior value (deleting only
                    # atoms that didn't exist before apply) - never blindly drop
                    # pre-existing show/season/episode atoms.
                    _restore_mp4_atoms(path_to_use, atom_snapshot)
                else:
                    # Legacy 0.2.0 journal row without a full atom snapshot.
                    # We can safely restore title/year (those WERE recorded),
                    # but we must NOT delete tvsh/tvsn/tves/stik/©ART: an old
                    # row can't tell whether those existed before MetaMatch
                    # touched the file, so deleting them could destroy the
                    # user's pre-existing metadata. Fail closed - leave the
                    # TV-specific atoms exactly as they are and warn.
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
                    result.setdefault("warnings", []).append(
                        "This file was tagged by an older MetaMatch version that didn't record the "
                        "original show/season/episode atoms, so they were left as-is on undo rather "
                        "than risk deleting metadata that may have pre-existed. Check them if needed.")

            result["restored_path"] = path_to_use
            self.journal.mark_rolled_back(txn.id)

            with self._lock:
                if current_path in self.episodes:
                    del self.episodes[current_path]
                refreshed = episode_scanner_module.read_episode(path_to_use)
                self.episodes[refreshed.path] = refreshed
                if current_path in self.order:
                    idx = self.order.index(current_path)
                    self.order[idx] = refreshed.path

        except Exception as e:
            result["error"] = str(e)
        return result

    # ------------------------------------------------------- duplicates
    def find_duplicates(self) -> dict:
        with self._lock:
            episodes = [self.episodes[p] for p in self.order]
        if not episodes:
            raise ValueError("Scan a folder first.")
        return {
            "exact": dedup_module.find_exact_duplicates(episodes),
            "probable": dedup_module.find_probable_duplicates_episodes(episodes),
        }

    def quarantine(self, paths: list[str]) -> dict:
        with self._lock:
            folder = self.folder
            valid_paths = [p for p in paths if p in self.episodes]
            fingerprints = {p: (self.episodes[p].size_bytes, self.episodes[p].mtime_ns, self.episodes[p].content_hash) for p in valid_paths}

        if not folder:
            raise ValueError("Scan a folder first.")
        if not paths:
            raise ValueError("No files selected.")

        rejected = [p for p in paths if p not in valid_paths]
        results = []

        # Hold each target file's mutation lock across its stale-check and move
        # (see MusicLibrary.quarantine for why).
        with _locked_paths(self._mutation_locks, valid_paths):
            still_valid = []
            for p in valid_paths:
                auth = _authority_error(folder, p)
                if auth:
                    results.append({
                        "original_path": p, "new_path": None,
                        "error": f"Refused to quarantine '{os.path.basename(p)}': {auth}.",
                    })
                    continue
                size, mtime_ns, content_hash = fingerprints[p]
                if _fingerprint_changed(p, size, mtime_ns, content_hash):
                    results.append({
                        "original_path": p, "new_path": None,
                        "error": "File changed on disk since it was scanned - rescan before quarantining.",
                    })
                elif os.path.isfile(p):
                    still_valid.append(p)

            all_paths = []
            for p in still_valid:
                all_paths.append(p)
                base = os.path.splitext(p)[0]
                for suffix in (".nfo", tv_tagger_module.THUMB_SUFFIX):
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
                if not r["error"] and r["original_path"] in self.episodes:
                    del self.episodes[r["original_path"]]
                    if r["original_path"] in self.order:
                        self.order.remove(r["original_path"])

        moved = sum(1 for r in results if not r["error"])
        return {"moved": moved, "results": results}

    # ------------------------------------------------- series metadata
    def _series_root_for(self, episode_path: str) -> str:
        """The show's root folder: the episode's parent, or its grandparent
        when the episode sits in a 'Season NN' subfolder."""
        parent = os.path.dirname(episode_path)
        if episode_scanner_module._SEASON_FOLDER_RE.match(os.path.basename(parent)):
            return os.path.dirname(parent)
        return parent

    def write_series_metadata(self, min_confidence: float = 75.0,
                              do_poster: bool = True, do_season_posters: bool = True) -> dict:
        """For each distinct series among the matched episodes, write a
        tvshow.nfo (and, optionally, a series poster + season posters) at the
        show's root folder. Each series is one journaled, rollback-protected
        transaction (kind 'tv_series'), so a mid-write failure leaves that
        series' folder exactly as it was."""
        min_confidence = _validate_confidence(min_confidence)
        with self._lock:
            eps = [self.episodes[p] for p in self.order]

        groups: dict = {}
        for e in eps:
            m = e.match or {}
            if not m.get("series_tmdb_id") or m.get("confidence", 0) < min_confidence:
                continue
            root = self._series_root_for(e.path)
            g = groups.setdefault((m["series_tmdb_id"], root),
                                  {"series_id": m["series_tmdb_id"], "root": root, "seasons": set()})
            if e.season is not None:
                g["seasons"].add(e.season)

        results = []
        for (series_id, root), g in groups.items():
            # Hold the series-root mutation lock for the whole write (fetch,
            # snapshot, journal, sidecar writes, commit) so it follows the same
            # transaction discipline as Apply and can't interleave with another
            # operation touching the same show folder.
            with self._mutation_locks.get(root):
                results.append(self._write_one_series(series_id, root, sorted(g["seasons"]),
                                                       do_poster, do_season_posters))

        succeeded = sum(1 for r in results if not r["error"])
        return {
            "series": len(results), "succeeded": succeeded, "failed": len(results) - succeeded,
            "results": results,
        }

    def _write_one_series(self, series_id: int, series_root: str, seasons: list,
                          do_poster: bool, do_season_posters: bool) -> dict:
        result = {
            "series_root": series_root, "series_id": series_id, "series_name": None,
            "error": None, "written": [], "warnings": [],
            "txn_id": None, "rolled_back": False, "recovery_required": False,
        }
        try:
            details = tv_matcher_module.fetch_series_details(series_id)
        except movie_matcher_module.TmdbNotConfigured as e:
            result["error"] = str(e)
            return result
        if not details:
            result["error"] = "Couldn't fetch series details from TMDB."
            return result
        result["series_name"] = details.get("name")

        # Re-check authority on the series root at write time: the root was
        # derived from an episode path recorded at scan, but the directory
        # could have been swapped for a junction/symlink pointing outside the
        # library since (the series-metadata analogue of the Apply TOCTOU).
        auth = _authority_error(self.folder, series_root)
        if auth:
            result["error"] = f"Refused to write series metadata into '{os.path.basename(series_root)}': {auth}."
            return result

        season_urls = {}
        if do_season_posters:
            for s in seasons:
                try:
                    url = tv_matcher_module.fetch_season_poster_url(series_id, s)
                except movie_matcher_module.TmdbNotConfigured as e:
                    result["error"] = str(e)
                    return result
                if url:
                    season_urls[s] = url

        nfo_path = tv_tagger_module.series_nfo_path(series_root)
        targets = [nfo_path]
        if do_poster and details.get("poster_url_full"):
            targets.append(tv_tagger_module.series_poster_path(series_root))
        for s in season_urls:
            targets.append(tv_tagger_module.season_poster_path(series_root, s))

        snapshot_artifacts = {}
        for t in targets:
            snap_bytes = _read_small_file(t, _MAX_SIDECAR_SNAPSHOT_BYTES)
            snapshot_artifacts[os.path.abspath(t)] = {
                "had": os.path.exists(t),
                "bytes_b64": _b64_or_none(snap_bytes),
            }
        before_state = {"artifacts": snapshot_artifacts}
        operation = {"type": "series_metadata", "series_root": series_root, "seasons": list(seasons)}

        txn_id = None
        try:
            txn_id = self.journal.begin("tv_series", nfo_path, nfo_path, before_state, operation, library_root=self.folder)
            result["txn_id"] = txn_id
            self.journal.mark_applying(txn_id)
        except Exception as e:
            result["error"] = (
                f"Couldn't record this operation in MetaMatch's journal ({e}); "
                "no series files were written, to stay safe."
            )
            return result

        written = []
        try:
            if _authority_error(self.folder, nfo_path, allow_missing=True):
                result["error"] = "The tvshow.nfo target isn't a safe path to write (symlink/reparse or escapes the library)."
                self.journal.mark_rolled_back(txn_id)
                result["rolled_back"] = True
                return result
            written.append(tv_tagger_module.write_tvshow_nfo(series_root, details))
            if do_poster and details.get("poster_url_full"):
                poster_dest = tv_tagger_module.series_poster_path(series_root)
                if _authority_error(self.folder, poster_dest, allow_missing=True):
                    result["warnings"].append("Series poster target isn't a safe path (symlink/escape); left untouched.")
                elif movie_tagger_module.sidecar_is_protected(poster_dest):
                    result["warnings"].append("Existing series poster too large to back up; left in place.")
                else:
                    p = tv_tagger_module.download_image(details["poster_url_full"], poster_dest)
                    if p:
                        written.append(p)
                    else:
                        result["warnings"].append("Series poster download failed (nfo still written).")
            for s, url in season_urls.items():
                season_dest = tv_tagger_module.season_poster_path(series_root, s)
                if _authority_error(self.folder, season_dest, allow_missing=True):
                    result["warnings"].append(f"Season {s} poster target isn't a safe path (symlink/escape); left untouched.")
                    continue
                if movie_tagger_module.sidecar_is_protected(season_dest):
                    result["warnings"].append(f"Existing season {s} poster too large to back up; left in place.")
                    continue
                p = tv_tagger_module.download_image(url, season_dest)
                if p:
                    written.append(p)
                else:
                    result["warnings"].append(f"Season {s} poster download failed.")

            result["written"] = written
            after = {"written": [os.path.abspath(w) for w in written], "fingerprints": {}}
            for w in written:
                fp = _file_fingerprint(w)
                if fp:
                    after["fingerprints"][os.path.abspath(w)] = fp
            self.journal.commit(txn_id, nfo_path, after_state=after)
        except Exception as e:
            result["error"] = str(e)
            _mark_rolling_back_safely(self.journal, txn_id)
            restored_ok, warns = _revert_series_artifacts(snapshot_artifacts)
            _finalize_failed_apply_safely(self.journal, txn_id, result, restored_ok, warns)

        return result

    def undo_series_metadata_all(self) -> dict:
        """Revert every series-metadata write for the CURRENT TV library
        (deletes tvshow.nfo/posters we created, restores any we overwrote),
        skipping artifacts the user has since changed. Scoped to self.folder
        so an Undo in one library can't reach into another that shares this
        (deliberately shared, persistent) journal."""
        if not self.folder:
            raise ValueError("Scan or select a TV library before undoing all series metadata.")
        txns = self.journal.list_undoable("tv_series", folder=self.folder)
        results = []
        restored_count = 0
        failed_count = 0
        for txn in txns:
            snap = (txn.before_state or {}).get("artifacts", {})
            fps = (txn.after_state or {}).get("fingerprints", {})
            ok, warns = _revert_series_artifacts(snap, fingerprints=fps)
            # Terminal state must reflect the actual outcome: only a clean
            # revert is ROLLED_BACK. A failed compensation left files on disk,
            # so it becomes RECOVERY_REQUIRED (with the warnings persisted) -
            # never terminalised as a successful rollback.
            if ok:
                self.journal.mark_rolled_back(txn.id)
                restored_count += 1
            else:
                self.journal.mark_recovery_required(
                    txn.id, {"note": "series-metadata undo couldn't fully revert files.",
                             "warnings": warns})
                failed_count += 1
            results.append({"series_root": (txn.operation or {}).get("series_root"),
                            "restored": ok, "warnings": warns})
        return {
            "attempted": len(results), "restored": restored_count, "failed": failed_count,
            "results": results,
        }

    # ----------------------------------------------------------- export
    def export_csv(self) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "file_path", "series_guess", "season", "episode",
            "matched_series", "matched_episode_title", "confidence", "tmdb_url",
        ])
        with self._lock:
            for p in self.order:
                e = self.episodes[p]
                m = e.match or {}
                writer.writerow([_csv_safe(val) for val in [
                    e.path, e.series_guess or "", e.season if e.season is not None else "",
                    e.episode if e.episode is not None else "",
                    m.get("series_name", ""), m.get("episode_title", ""),
                    m.get("confidence", ""), m.get("tmdb_url", ""),
                ]])
        return buf.getvalue()
