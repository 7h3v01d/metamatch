"""
test_fault_injection.py
Adversarial fault-injection matrix.

test_rollback.py established the core guarantee (a failed apply rolls back
to its captured before-state). This file widens the fault surface to the
cases the review's closing note called for - the failures that don't come
from our own code raising, but from the environment turning hostile
underneath a half-finished operation:

  * the disk fills mid-write (ENOSPC) or a file goes read-only (EACCES),
    at different points in the mutation sequence;
  * ffmpeg is killed part-way through a remux;
  * the journal database is locked by another writer at a commit boundary;
  * the process dies at each individual journal state and restarts;
  * a journal row is found corrupt (a torn or truncated write) at startup.

The recurring assertion is the same one that matters for a tool that edits
irreplaceable media in place: after any of these, either the original file
is intact and the journal says so, or the operation is loudly flagged
RECOVERY_REQUIRED - never silently half-done, and never a crash that takes
the whole library (or startup recovery) down with it.

Faults are injected two ways. Real I/O faults (ENOSPC, EACCES) are raised
from the real write functions, so the actual rollback code runs against a
real partially-mutated file. Environmental faults that are hard to trigger
deterministically (a killed subprocess, a locked database, a crash at an
exact instruction) are injected by monkeypatching the precise seam, which
is what lets these tests be repeatable rather than timing-dependent.
"""

from __future__ import annotations

import errno
import glob
import hashlib
import os
import sqlite3
import subprocess
import types

import pytest

from conftest import requires_ffmpeg
from metamatch.journal import (
    Journal, APPLYING, COMMITTED, INTERRUPTED, PENDING,
    RECOVERY_REQUIRED, ROLLING_BACK, ROLLED_BACK,
)


def _enospc(*args, **kwargs):
    raise OSError(errno.ENOSPC, "No space left on device")


def _eacces(*args, **kwargs):
    raise PermissionError(errno.EACCES, "Permission denied")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _untagged_track(lib):
    """The scene-named mp3 that starts with no tags, so restoring its
    (empty) pre-apply tags on rollback is observable."""
    return [t for t in lib.tracks_payload() if t["filename"].startswith("01 -")][0]


def _has_id3(path: str) -> bool:
    from mutagen.id3 import ID3, ID3NoHeaderError
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        return False
    return ("TPE1" in tags) or ("TIT2" in tags)


def _apic_count(path: str) -> int:
    from mutagen.id3 import ID3, ID3NoHeaderError
    try:
        return len(ID3(path).getall("APIC"))
    except ID3NoHeaderError:
        return 0


# ---------------------------------------------------------------------------
# Disk-full and permission faults, injected at different points in the
# music mutation sequence (tags -> art -> rename).
# ---------------------------------------------------------------------------

class TestDiskFullAndPermission:
    @requires_ffmpeg
    def test_enospc_at_first_mutation_leaves_file_pristine(
        self, music_dir, mock_music_match, monkeypatch
    ):
        """Disk fills at the very first write (tags). Nothing landed, so the
        rollback is a no-op - but the transaction must still end ROLLED_BACK
        and the file must be exactly as it started."""
        from metamatch import MusicLibrary
        import metamatch.tagger as tagger_module

        lib = MusicLibrary()
        lib.scan(str(music_dir))
        lib.match()
        target = _untagged_track(lib)
        before_hash = _sha256(target["path"])

        # apply_tags is the forward tag write; set_or_clear_tags is the
        # rollback restore, which must stay intact so recovery can run.
        monkeypatch.setattr(tagger_module, "apply_tags", _enospc)
        result = lib.apply(target["id"], do_tag=True, do_rename=True, do_art=False)

        assert result["error"]
        assert result["rolled_back"] is True
        assert result["recovery_required"] is False
        assert os.path.exists(target["path"])          # never renamed
        assert _sha256(target["path"]) == before_hash  # byte-for-byte unchanged
        assert lib.journal.get(result["txn_id"]).status == ROLLED_BACK

    @requires_ffmpeg
    def test_enospc_during_art_rolls_back_written_tags(
        self, music_dir, mock_music_match, mock_cover_art, monkeypatch
    ):
        """Tags are written, THEN the disk fills as the cover art is embedded.
        Rollback has real work to do: strip the just-written tags back to the
        (empty) pre-apply state and leave no art behind."""
        from metamatch import MusicLibrary
        import metamatch.tagger as tagger_module

        lib = MusicLibrary()
        lib.scan(str(music_dir))
        lib.match()
        target = _untagged_track(lib)
        assert not _has_id3(target["path"])  # precondition: starts untagged

        monkeypatch.setattr(tagger_module, "embed_cover_art", _enospc)
        result = lib.apply(target["id"], do_tag=True, do_rename=True, do_art=True)

        assert result["error"]
        assert result["rolled_back"] is True
        assert result["recovery_required"] is False
        assert not _has_id3(target["path"])     # tags rolled back to empty
        assert _apic_count(target["path"]) == 0  # no art left behind
        assert os.path.exists(target["path"])   # never renamed
        assert lib.journal.get(result["txn_id"]).status == ROLLED_BACK

    @requires_ffmpeg
    def test_eacces_during_nfo_rolls_back_movie_embed(
        self, movie_dir, mock_movie_match, monkeypatch
    ):
        """A movie's metadata atoms are embedded, then the .nfo write hits a
        read-only directory (EACCES). Rollback must revert the embedded atoms
        and leave no sidecar - the file returns to untitled, in place."""
        from metamatch import MovieLibrary
        import metamatch.movie_tagger as movie_tagger_module
        from mutagen.mp4 import MP4

        lib = MovieLibrary()
        lib.scan(str(movie_dir))
        lib.match()
        target = [v for v in lib.videos_payload() if v["filename"].endswith(".mp4")][0]
        assert MP4(target["path"]).get("\xa9nam") is None  # starts untitled

        monkeypatch.setattr(movie_tagger_module, "write_nfo", _eacces)
        result = lib.apply(target["id"], do_tag=True, do_rename=True, do_nfo=True, do_poster=False)

        assert result["error"]
        assert result["rolled_back"] is True
        assert result["recovery_required"] is False
        assert os.path.exists(target["path"])                 # never renamed
        assert MP4(target["path"]).get("\xa9nam") is None      # embed reverted
        nfo = os.path.splitext(target["path"])[0] + ".nfo"
        assert not os.path.exists(nfo)                         # no partial sidecar
        assert lib.journal.get(result["txn_id"]).status == ROLLED_BACK


# ---------------------------------------------------------------------------
# ffmpeg killed mid-remux. The remux writes to a hidden temp file and only
# os.replace()s it into place on success, so a killed ffmpeg must leave the
# ORIGINAL untouched, clean up its temp file, and surface as a rollback.
# ---------------------------------------------------------------------------

class TestFfmpegRemuxFaults:
    def _killed_ffmpeg(self, real_run):
        """A subprocess.run stand-in that simulates ffmpeg being killed after
        it had started writing its output temp file (leaving a partial file
        behind), while passing ffprobe and everything else through to the
        real implementation."""
        def fake_run(cmd, **kwargs):
            is_ffmpeg_remux = cmd and cmd[0] == "ffmpeg" and "-map" in cmd
            if is_ffmpeg_remux:
                tmp_out = cmd[-1]
                with open(tmp_out, "wb") as f:
                    f.write(b"\x00\x00truncated-partial-remux")
                return types.SimpleNamespace(returncode=-9, stdout=b"", stderr=b"Killed")
            return real_run(cmd, **kwargs)
        return fake_run

    @requires_ffmpeg
    def test_killed_ffmpeg_leaves_original_intact_at_unit_level(self, movie_dir, monkeypatch):
        import metamatch.movie_tagger as movie_tagger_module

        mkv = str(movie_dir / "Test.Movie.2020.1080p.BluRay.x264-GROUP.mkv")
        before_hash = _sha256(mkv)

        monkeypatch.setattr(
            movie_tagger_module.subprocess, "run",
            self._killed_ffmpeg(subprocess.run),
        )
        with pytest.raises(RuntimeError):
            movie_tagger_module._embed_via_ffmpeg_remux(mkv, {"title": "Test Movie", "year": "2020"})

        assert _sha256(mkv) == before_hash  # original never replaced
        # the partial temp file was cleaned up, not orphaned
        leftovers = glob.glob(str(movie_dir / ".*metamatch_tmp*"))
        assert leftovers == []

    @requires_ffmpeg
    def test_killed_ffmpeg_rolls_back_through_apply(
        self, movie_dir, mock_movie_match, monkeypatch
    ):
        from metamatch import MovieLibrary
        import metamatch.movie_tagger as movie_tagger_module

        monkeypatch.setattr(
            movie_tagger_module.subprocess, "run",
            self._killed_ffmpeg(subprocess.run),
        )
        lib = MovieLibrary()
        lib.scan(str(movie_dir))
        lib.match()
        target = [v for v in lib.videos_payload() if v["filename"].endswith(".mkv")][0]
        before_hash = _sha256(target["path"])

        result = lib.apply(target["id"], do_tag=True, do_rename=True, do_nfo=False, do_poster=False)

        assert result["error"]
        assert result["rolled_back"] is True
        assert os.path.exists(target["path"])            # original still there
        assert _sha256(target["path"]) == before_hash    # and unchanged
        assert glob.glob(str(movie_dir / ".*metamatch_tmp*")) == []  # no orphan temp
        assert lib.journal.get(result["txn_id"]).status == ROLLED_BACK


# ---------------------------------------------------------------------------
# Journal database locked by a competing writer.
# ---------------------------------------------------------------------------

class TestDbLockContention:
    def test_locked_db_raises_cleanly_without_phantom_row(self, tmp_path, monkeypatch):
        """A second connection holds an EXCLUSIVE lock. A journal write should
        time out and raise OperationalError rather than hang forever or half-
        write - and crucially leave no partial row behind (the INSERT's
        transaction rolls back)."""
        jr = Journal(str(tmp_path / "j.sqlite"))

        holder = sqlite3.connect(jr.path, timeout=0.1)
        holder.isolation_level = None
        holder.execute("BEGIN EXCLUSIVE")
        try:
            # shrink the journal's own busy-wait so the test doesn't sit for 10s
            def short_connect():
                conn = sqlite3.connect(jr.path, timeout=0.2)
                conn.row_factory = sqlite3.Row
                return conn
            monkeypatch.setattr(jr, "_connect", short_connect)

            with pytest.raises(sqlite3.OperationalError):
                jr.begin("music", "/x/a.mp3", "/x/a.mp3", {"artist": "A"}, {"do_tag": True})
        finally:
            holder.rollback()
            holder.close()

        # lock released: the journal is usable and has no leftover pending row
        assert jr.find_incomplete("music") == []
        assert jr.list_undoable("music") == []

    @requires_ffmpeg
    def test_lock_at_commit_boundary_recovers_as_required(
        self, music_dir, mock_music_match, monkeypatch
    ):
        """The nastier case: the file mutations and rename SUCCEED, then the
        journal is locked exactly at the commit. The apply raises, the row is
        left APPLYING, and a restart must escalate it to RECOVERY_REQUIRED so
        the (now-renamed) file isn't silently forgotten."""
        from metamatch import MusicLibrary

        lib = MusicLibrary()
        lib.scan(str(music_dir))
        lib.match()
        target = _untagged_track(lib)
        journal_path = lib.journal.path

        def locked_commit(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")
        monkeypatch.setattr(lib.journal, "commit", locked_commit)

        with pytest.raises(sqlite3.OperationalError):
            lib.apply(target["id"], do_tag=True, do_rename=True, do_art=False)

        # the row is stranded mid-apply until a restart runs recovery
        assert lib.journal.find_in_progress("music")  # still APPLYING

        restarted = MusicLibrary(journal=Journal(journal_path))
        notices = restarted.get_recovery_notices()
        assert any(n["status"] == RECOVERY_REQUIRED for n in notices)


# ---------------------------------------------------------------------------
# Process death at each individual journal boundary, then restart. "Restart"
# is a fresh Library built on the same journal file + folder, which runs
# recover() in its constructor - the real startup path.
# ---------------------------------------------------------------------------

class TestBoundaryDeathRecovery:
    def _journal(self, tmp_path):
        return Journal(str(tmp_path / "j.sqlite"))

    def _restart(self, journal_path):
        from metamatch import MusicLibrary
        return MusicLibrary(journal=Journal(journal_path))

    def test_death_at_pending_is_benign_interrupted(self, tmp_path):
        """Died between begin() and the first file mutation: nothing on disk
        was touched, so it's the benign INTERRUPTED notice."""
        jr = self._journal(tmp_path)
        jr.begin("music", "/x/a.mp3", "/x/a.mp3", {"artist": "A"}, {"do_tag": True})

        notices = self._restart(jr.path).get_recovery_notices()
        assert len(notices) == 1
        assert notices[0]["status"] == INTERRUPTED

    def test_death_while_applying_needs_recovery(self, tmp_path):
        """Died with file mutations possibly half-done: unsafe, so it must be
        escalated to RECOVERY_REQUIRED, not quietly dropped."""
        jr = self._journal(tmp_path)
        tid = jr.begin("music", "/x/a.mp3", "/x/a.mp3", {"artist": "A"}, {"do_tag": True})
        jr.mark_applying(tid)

        notices = self._restart(jr.path).get_recovery_notices()
        assert len(notices) == 1
        assert notices[0]["status"] == RECOVERY_REQUIRED

    def test_death_while_rolling_back_needs_recovery(self, tmp_path):
        """Died mid-rollback (compensations partly run): also RECOVERY_REQUIRED,
        for the same reason - on-disk state is unverified."""
        jr = self._journal(tmp_path)
        tid = jr.begin("music", "/x/a.mp3", "/x/a.mp3", {"artist": "A"}, {"do_tag": True})
        jr.mark_applying(tid)
        jr.mark_rolling_back(tid)

        notices = self._restart(jr.path).get_recovery_notices()
        assert len(notices) == 1
        assert notices[0]["status"] == RECOVERY_REQUIRED

    def test_death_after_commit_survives_and_is_undoable(self, tmp_path):
        """Died AFTER the commit landed: this is a completed operation, not a
        recovery case. It should generate no notice and remain undoable."""
        jr = self._journal(tmp_path)
        tid = jr.begin("music", "/x/a.mp3", "/x/a.mp3", {"artist": "A"}, {"do_tag": True})
        jr.mark_applying(tid)
        jr.commit(tid, "/x/a.mp3")

        restarted = self._restart(jr.path)
        assert restarted.get_recovery_notices() == []
        assert "/x/a.mp3" in restarted.journal.get_undoable_paths("music")

    def test_recovery_is_idempotent_across_two_restarts(self, tmp_path):
        """Recovering the same journal twice must not re-flag or duplicate
        anything the first restart already resolved."""
        jr = self._journal(tmp_path)
        p_tid = jr.begin("music", "/x/p.mp3", "/x/p.mp3", {}, {})
        a_tid = jr.begin("music", "/x/a.mp3", "/x/a.mp3", {}, {})
        jr.mark_applying(a_tid)

        first = self._restart(jr.path).get_recovery_notices()
        second = self._restart(jr.path).get_recovery_notices()
        assert len(first) == 2
        assert second == []


# ---------------------------------------------------------------------------
# Corrupt journal row (torn/truncated write, bit flip). Must never crash
# enumeration or startup recovery; the bad row is escalated + quarantined.
# ---------------------------------------------------------------------------

class TestCorruptJournalRow:
    def _corrupt_before_state(self, journal_path, txn_id):
        conn = sqlite3.connect(journal_path)
        conn.execute("UPDATE transactions SET before_state=? WHERE id=?", ("{not valid json", txn_id))
        conn.commit()
        conn.close()

    def test_corrupt_row_does_not_crash_startup_recovery(self, tmp_path):
        jr = Journal(str(tmp_path / "j.sqlite"))
        tid = jr.begin("music", "/x/a.mp3", "/x/a.mp3", {"artist": "A"}, {"do_tag": True})
        jr.mark_applying(tid)
        self._corrupt_before_state(jr.path, tid)

        # constructing a Library runs recover() - it must not raise
        from metamatch import MusicLibrary
        lib = MusicLibrary(journal=Journal(jr.path))
        notices = lib.get_recovery_notices()
        assert any(n["status"] == RECOVERY_REQUIRED for n in notices)

    def test_corrupt_row_is_quarantined_and_idempotent(self, tmp_path):
        jr = Journal(str(tmp_path / "j.sqlite"))
        tid = jr.begin("music", "/x/a.mp3", "/x/a.mp3", {"artist": "A"}, {"do_tag": True})
        jr.mark_applying(tid)
        self._corrupt_before_state(jr.path, tid)

        first = jr.recover("music")
        assert len(first) == 1
        assert first[0].status == RECOVERY_REQUIRED
        # the raw bad bytes are preserved for forensics
        assert "{not valid json" in (first[0].rollback_info or {}).get("corrupt_before_state", "")

        # second recovery must not re-flag the now-quarantined row
        assert jr.recover("music") == []
        # and the row now parses cleanly, sitting at its terminal state
        assert jr.get(tid).status == RECOVERY_REQUIRED

    def test_corrupt_row_does_not_hide_healthy_undo_history(self, tmp_path):
        jr = Journal(str(tmp_path / "j.sqlite"))
        good = jr.begin("music", "/x/good.mp3", "/x/good.mp3", {"artist": "G"}, {"do_tag": True})
        jr.commit(good, "/x/good.mp3")
        bad = jr.begin("music", "/x/bad.mp3", "/x/bad.mp3", {"artist": "B"}, {"do_tag": True})
        jr.mark_applying(bad)
        self._corrupt_before_state(jr.path, bad)

        # the corrupt row must not break enumeration of the healthy one
        assert jr.get_undoable_paths("music") == {"/x/good.mp3"}
        jr.recover("music")
        assert jr.get_undoable_paths("music") == {"/x/good.mp3"}


# ---------------------------------------------------------------------------
# Orphaned remux temp files. A whole-process kill mid-remux can't run the
# temp cleanup, so a hidden .<name>.metamatch_tmp.<ext> is left behind. The
# next scan sweeps clearly-stale ones, while never touching a temp that could
# be an in-progress remux (age-guarded).
# ---------------------------------------------------------------------------

class TestOrphanRemuxTempSweep:
    def _make_temp(self, folder, name, age_seconds):
        import time
        p = os.path.join(folder, name)
        with open(p, "wb") as f:
            f.write(b"partial remux bytes")
        old = time.time() - age_seconds
        os.utime(p, (old, old))
        return p

    def test_stale_orphan_is_swept(self, tmp_path):
        import metamatch.movie_tagger as mt
        folder = str(tmp_path)
        orphan = self._make_temp(folder, ".Some.Movie.metamatch_tmp.mkv", age_seconds=600)
        removed = mt.sweep_orphan_remux_temps(folder)
        assert orphan in removed
        assert not os.path.exists(orphan)

    def test_fresh_temp_is_left_alone(self, tmp_path):
        """A temp younger than the age threshold might be an active remux
        writing right now - it must never be deleted out from under ffmpeg."""
        import metamatch.movie_tagger as mt
        folder = str(tmp_path)
        fresh = self._make_temp(folder, ".Some.Movie.metamatch_tmp.mkv", age_seconds=1)
        removed = mt.sweep_orphan_remux_temps(folder)
        assert removed == []
        assert os.path.exists(fresh)

    def test_normal_files_are_never_touched(self, tmp_path):
        import metamatch.movie_tagger as mt
        folder = str(tmp_path)
        real = self._make_temp(folder, "Some.Movie.mkv", age_seconds=600)  # no marker
        removed = mt.sweep_orphan_remux_temps(folder)
        assert removed == []
        assert os.path.exists(real)

    def test_missing_folder_never_raises(self):
        import metamatch.movie_tagger as mt
        assert mt.sweep_orphan_remux_temps("/no/such/folder/anywhere") == []

    @requires_ffmpeg
    def test_scan_sweeps_stale_orphan(self, movie_dir):
        """End to end: a stale orphan sitting in a real movie folder is gone
        after a scan, and the scan records what it removed."""
        from metamatch import MovieLibrary
        orphan = self._make_temp(str(movie_dir), ".Ghost.metamatch_tmp.mkv", age_seconds=600)
        lib = MovieLibrary()
        lib.scan(str(movie_dir))
        assert not os.path.exists(orphan)
        assert orphan in lib.swept_orphan_temps


# ---------------------------------------------------------------------------
# Enriched /api/recovery: severity + human message per notice, and a
# needs_attention list that persists across restarts (not just the one boot
# that flagged it).
# ---------------------------------------------------------------------------

class TestRecoveryEndpointEnrichment:
    def _seed_recovery_required(self, journal_path):
        """Drive a transaction to RECOVERY_REQUIRED directly in the journal."""
        jr = Journal(journal_path)
        tid = jr.begin("music", "/tmp/x.mp3", "/tmp/x.mp3", {"artist": "A"}, {"do_tag": True})
        jr.mark_applying(tid)
        jr.mark_recovery_required(tid, {"note": "compensation failed in a test"})
        return tid

    def test_needs_attention_persists_and_is_flagged(self, app_client):
        import app as app_module
        self._seed_recovery_required(app_module.music_library.journal.path)

        data = app_client.get("/api/recovery").get_json()
        assert data["summary"]["needs_attention"] is True
        assert data["summary"]["recovery_required"] >= 1
        kinds_paths = {(n["kind"], n["original_path"]) for n in data["needs_attention"]}
        assert ("music", "/tmp/x.mp3") in kinds_paths

    def test_notices_carry_severity_and_message(self, app_client):
        import app as app_module
        # a benign pending row, surfaced via a simulated startup recovery
        Journal(app_module.music_library.journal.path).begin(
            "music", "/tmp/benign.mp3", "/tmp/benign.mp3", {}, {"do_tag": True},
        )
        app_module.music_library.recovered_transactions = (
            app_module.music_library.journal.recover("music")
        )
        data = app_client.get("/api/recovery").get_json()
        assert len(data["music"]) == 1
        notice = data["music"][0]
        assert notice["severity"] == "info"          # interrupted, not serious
        assert notice["message"]                       # human-readable text present

    def test_recovery_required_notice_marked_attention(self, app_client):
        import app as app_module
        # left APPLYING -> this boot's recover() escalates it to
        # RECOVERY_REQUIRED, so it appears in the startup notices as 'attention'
        jr = Journal(app_module.music_library.journal.path)
        tid = jr.begin("music", "/tmp/mid.mp3", "/tmp/mid.mp3", {"artist": "A"}, {"do_tag": True})
        jr.mark_applying(tid)
        app_module.music_library.recovered_transactions = (
            app_module.music_library.journal.recover("music")
        )
        data = app_client.get("/api/recovery").get_json()
        serious = [n for n in data["music"] if n["severity"] == "attention"]
        assert serious and all(n["message"] for n in serious)
