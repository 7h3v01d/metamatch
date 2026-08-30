"""
test_rollback.py
Fault-injection tests for automatic rollback.

Everything else in the suite checks that operations SUCCEED correctly.
These check what happens when an apply FAILS partway: the journal already
recorded intent, one or more file mutations already landed, and then a
later mutation raises. The guarantee under test is the one the adversarial
review named as the remaining frontier:

    Apply either completes, or MetaMatch returns the file to its
    captured before-state (ROLLED_BACK) - and if a compensation that was
    expected to work doesn't, the transaction is flagged RECOVERY_REQUIRED
    rather than left silently inconsistent.

Faults are injected by monkeypatching the LAST mutation of each apply
(rename) to raise, since rename runs after tags/art/nfo/poster - so at the
point it fails, real partial work exists that rollback must undo.
"""

from __future__ import annotations

import os

import pytest

from conftest import requires_ffmpeg
from metamatch.journal import (
    Journal, APPLYING, COMMITTED, INTERRUPTED, PENDING,
    RECOVERY_REQUIRED, ROLLED_BACK,
)


def _boom(*args, **kwargs):
    raise OSError("simulated failure during rename")


# ---------------------------------------------------------------------------
# Music
# ---------------------------------------------------------------------------

class TestMusicApplyRollback:
    def _messy_track(self, lib):
        """The untagged, scene-named mp3 - it starts with no tags, so a
        rollback that restores its (empty) pre-apply tags is observable."""
        return [t for t in lib.tracks_payload()
                if t["filename"].startswith("01 -")][0]

    def test_rename_failure_rolls_back_written_tags(self, music_dir, mock_music_match, monkeypatch):
        from metamatch import MusicLibrary
        import metamatch.tagger as tagger_module
        from mutagen.easyid3 import EasyID3
        from mutagen.id3 import ID3NoHeaderError

        lib = MusicLibrary()
        lib.scan(str(music_dir))
        lib.match()
        target = self._messy_track(lib)

        monkeypatch.setattr(tagger_module, "rename_to_match", _boom)
        result = lib.apply(target["id"], do_tag=True, do_rename=True)

        # The apply failed, but was rolled back cleanly.
        assert result["error"] is not None
        assert result["rolled_back"] is True
        assert result["recovery_required"] is False

        # File never moved (rename is the failing/last step) and the tags
        # we wrote were stripped back to the file's empty pre-apply state.
        assert os.path.exists(target["id"])
        try:
            tags = EasyID3(target["id"])
            assert "artist" not in tags
            assert "title" not in tags
        except ID3NoHeaderError:
            pass  # no tag header at all is also "no tags" - fine

        # Journal row reflects the terminal state, not a bare "failed".
        txn = lib.journal.get(result["txn_id"])
        assert txn.status == ROLLED_BACK
        assert txn.rollback_info["apply_error"]

    def test_rename_failure_strips_art_it_embedded(self, music_dir, mock_music_match, mock_cover_art, monkeypatch):
        from metamatch import MusicLibrary
        import metamatch.tagger as tagger_module
        from mutagen.id3 import ID3, ID3NoHeaderError

        lib = MusicLibrary()
        lib.scan(str(music_dir))
        lib.match()
        # tagged.mp3 has an ID3 header but no embedded art to begin with.
        target = [t for t in lib.tracks_payload() if t["filename"] == "tagged.mp3"][0]

        monkeypatch.setattr(tagger_module, "rename_to_match", _boom)
        result = lib.apply(target["id"], do_tag=True, do_rename=True, do_art=True)

        assert result["error"] is not None
        assert result["rolled_back"] is True
        # Art was embedded then stripped on rollback (file had none before).
        try:
            tags = ID3(target["id"])
            assert not tags.getall("APIC")
        except ID3NoHeaderError:
            pass

    def test_failed_compensation_marks_recovery_required(self, music_dir, mock_music_match, monkeypatch):
        from metamatch import MusicLibrary
        import metamatch.tagger as tagger_module

        lib = MusicLibrary()
        lib.scan(str(music_dir))
        lib.match()
        target = self._messy_track(lib)

        # Rename fails (triggers rollback), AND the tag-restore compensation
        # itself fails - a genuinely inconsistent file that needs a human.
        monkeypatch.setattr(tagger_module, "rename_to_match", _boom)
        monkeypatch.setattr(tagger_module, "set_or_clear_tags", _boom)
        result = lib.apply(target["id"], do_tag=True, do_rename=True)

        assert result["error"] is not None
        assert result["rolled_back"] is False
        assert result["recovery_required"] is True

        txn = lib.journal.get(result["txn_id"])
        assert txn.status == RECOVERY_REQUIRED
        assert txn.rollback_info["apply_error"]
        assert txn.rollback_info["warnings"]

    def test_successful_apply_is_not_flagged_as_rolled_back(self, music_dir, mock_music_match):
        from metamatch import MusicLibrary

        lib = MusicLibrary()
        lib.scan(str(music_dir))
        lib.match()
        target = self._messy_track(lib)

        result = lib.apply(target["id"], do_tag=True, do_rename=True)

        assert result["error"] is None
        assert result["rolled_back"] is False
        assert result["recovery_required"] is False
        assert lib.journal.get(result["txn_id"]).status == COMMITTED


# ---------------------------------------------------------------------------
# Movie
# ---------------------------------------------------------------------------

@requires_ffmpeg
class TestMovieApplyRollback:
    def _mp4(self, lib):
        return [v for v in lib.videos_payload() if v["filename"] == "sample_movie.mp4"][0]

    def test_rename_failure_removes_freshly_created_nfo(self, movie_dir, mock_movie_match, monkeypatch):
        from metamatch import MovieLibrary
        import metamatch.movie_tagger as movie_tagger_module

        lib = MovieLibrary()
        lib.scan(str(movie_dir))
        lib.match()
        target = self._mp4(lib)

        monkeypatch.setattr(movie_tagger_module, "rename_to_match", _boom)
        result = lib.apply(target["id"], do_tag=False, do_rename=True, do_nfo=True, do_poster=False)

        assert result["error"] is not None
        assert result["rolled_back"] is True
        # The .nfo we created was removed, and the video never moved.
        assert result["nfo_path"] and not os.path.exists(result["nfo_path"])
        assert os.path.exists(target["id"])
        assert lib.journal.get(result["txn_id"]).status == ROLLED_BACK

    def test_rename_failure_restores_overwritten_nfo(self, movie_dir, mock_movie_match, monkeypatch):
        from metamatch import MovieLibrary
        import metamatch.movie_tagger as movie_tagger_module

        # A pre-existing .nfo next to the video, with content MetaMatch will
        # overwrite when it writes its own during apply.
        nfo_path = os.path.splitext(self_path := str(movie_dir / "sample_movie.mp4"))[0] + ".nfo"
        original = "<movie><title>Do not lose me</title></movie>"
        with open(nfo_path, "w", encoding="utf-8") as f:
            f.write(original)

        lib = MovieLibrary()
        lib.scan(str(movie_dir))
        lib.match()
        target = self._mp4(lib)

        monkeypatch.setattr(movie_tagger_module, "rename_to_match", _boom)
        result = lib.apply(target["id"], do_tag=False, do_rename=True, do_nfo=True, do_poster=False)

        assert result["rolled_back"] is True
        # The user's original .nfo content is back, not MetaMatch's version.
        with open(nfo_path, encoding="utf-8") as f:
            assert f.read() == original

    def test_rename_failure_reverts_mp4_embedded_atoms(self, movie_dir, mock_movie_match, monkeypatch):
        from metamatch import MovieLibrary
        import metamatch.movie_tagger as movie_tagger_module
        from mutagen.mp4 import MP4

        lib = MovieLibrary()
        lib.scan(str(movie_dir))
        lib.match()
        target = self._mp4(lib)

        # mp4 embedding edits atoms directly (no ffmpeg), and is reversible.
        monkeypatch.setattr(movie_tagger_module, "rename_to_match", _boom)
        result = lib.apply(target["id"], do_tag=True, do_rename=True, do_nfo=False, do_poster=False)

        assert result["rolled_back"] is True
        atoms = MP4(target["id"])
        assert "\xa9nam" not in atoms  # title atom that apply added is gone again

    def test_irreversible_remux_is_flagged_recovery_required(self, movie_dir, mock_movie_match, monkeypatch):
        from metamatch import MovieLibrary
        import metamatch.movie_tagger as movie_tagger_module
        from metamatch.journal import RECOVERY_REQUIRED

        lib = MovieLibrary()
        lib.scan(str(movie_dir))
        lib.match()
        # The .mkv goes through an ffmpeg remux to embed metadata, which can't
        # be reverted. Since the file still carries the applied metadata, the
        # rollback is NOT clean - it must be flagged RECOVERY_REQUIRED, not
        # falsely reported as ROLLED_BACK (which must mean before-state
        # restored). This keeps the journal's state names trustworthy.
        target = [v for v in lib.videos_payload()
                  if v["filename"].endswith(".mkv")][0]

        monkeypatch.setattr(movie_tagger_module, "rename_to_match", _boom)
        result = lib.apply(target["id"], do_tag=True, do_rename=True, do_nfo=False, do_poster=False)

        assert result["error"] is not None
        assert result["rolled_back"] is False
        assert result["recovery_required"] is True
        assert any("remux" in w.lower() for w in result.get("warnings", []))
        assert lib.journal.get(result["txn_id"]).status == RECOVERY_REQUIRED


# ---------------------------------------------------------------------------
# Crash recovery: process death at a journal boundary
# ---------------------------------------------------------------------------

class TestCrashRecoveryStates:
    def test_leftover_applying_row_becomes_recovery_required(self, tmp_path):
        path = str(tmp_path / "j.sqlite")
        j = Journal(path)
        txn_id = j.begin("music", "/a.mp3", "/a.mp3", {}, {})
        j.mark_applying(txn_id)  # died here: file mutations may be half-done

        # Simulate a fresh process opening the same journal on restart.
        restarted = Journal(path)
        recovered = restarted.recover("music")

        assert [t.id for t in recovered] == [txn_id]
        assert recovered[0].status == RECOVERY_REQUIRED
        persisted = restarted.get(txn_id)
        assert persisted.status == RECOVERY_REQUIRED
        assert persisted.rollback_info is not None

    def test_leftover_pending_row_stays_benign_interrupted(self, tmp_path):
        path = str(tmp_path / "j.sqlite")
        j = Journal(path)
        txn_id = j.begin("music", "/a.mp3", "/a.mp3", {}, {})  # died before any mutation

        recovered = Journal(path).recover("music")

        assert [t.id for t in recovered] == [txn_id]
        # Nothing was written to disk yet, so this is the benign notice,
        # NOT the escalated recovery-required state.
        assert recovered[0].status == INTERRUPTED

    def test_recover_is_idempotent_across_both_states(self, tmp_path):
        path = str(tmp_path / "j.sqlite")
        j = Journal(path)
        pending_id = j.begin("music", "/a.mp3", "/a.mp3", {}, {})
        applying_id = j.begin("music", "/b.mp3", "/b.mp3", {}, {})
        j.mark_applying(applying_id)

        first = Journal(path).recover("music")
        second = Journal(path).recover("music")

        assert {t.id for t in first} == {pending_id, applying_id}
        assert second == []  # already resolved, nothing left mid-flight
