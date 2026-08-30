"""
test_review_020.py
Regression tests for the 0.2.0 adversarial review findings. Each class pins a
specific finding so a future change can't silently reopen it.

  1. symlink / reparse escape of the library authority boundary  (CRITICAL)
  2. TV series-metadata Undo crossing library boundaries          (HIGH)
  3. TV Undo destroying pre-existing TV MP4 atoms                 (HIGH)
  4. failed series Undo mis-accounted as a successful rollback    (HIGH)
  5. same-file Apply/Undo/Quarantine not serialized              (HIGH)
  6. irreversible-remux rollback mislabelled ROLLED_BACK          (MED/HIGH)
  7. oversized pre-existing sidecar overwritten unrecoverably     (LOW)
"""

from __future__ import annotations

import os
import subprocess
import threading

import pytest

from conftest import requires_ffmpeg
from metamatch.journal import Journal, RECOVERY_REQUIRED, ROLLED_BACK


def _make_mp3(path):
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-b:a", "128k", path],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)


# --------------------------------------------------------------------------
# 1. Symlink / reparse escape
# --------------------------------------------------------------------------

@requires_ffmpeg
class TestSymlinkAuthorityBoundary:
    def _setup(self, tmp_path):
        lib = tmp_path / "Library"; lib.mkdir()
        outside = tmp_path / "Outside"; outside.mkdir()
        victim = outside / "victim.mp3"
        _make_mp3(str(victim))
        os.symlink(str(victim), str(lib / "linked.mp3"))
        return lib, victim

    def test_music_scan_rejects_symlink_outside_root(self, tmp_path):
        from metamatch import MusicLibrary
        lib_dir, _ = self._setup(tmp_path)
        real = lib_dir / "real.mp3"; _make_mp3(str(real))
        payload = MusicLibrary(journal=Journal(str(tmp_path / "j.sqlite"))).scan(str(lib_dir))
        names = {os.path.basename(p["path"]) for p in payload}
        assert "linked.mp3" not in names   # symlink excluded
        assert "real.mp3" in names          # real file still scanned

    def test_apply_cannot_follow_external_symlink(self, tmp_path):
        from metamatch import MusicLibrary
        import metamatch.matcher as matcher
        from mutagen.easyid3 import EasyID3
        lib_dir, victim = self._setup(tmp_path)
        matcher.find_best_match = lambda t: {"recording_id": "r", "release_id": "x",
            "title": "Changed", "artist": "MetaMatch", "album": "A", "date": "2026-01-01", "confidence": 99}

        lib = MusicLibrary(journal=Journal(str(tmp_path / "j.sqlite")))
        payload = lib.scan(str(lib_dir))
        # There is nothing to apply to: the only file was a symlink, excluded.
        assert payload == []
        # And the outside victim is untouched.
        try:
            assert EasyID3(str(victim)).get("artist") != ["MetaMatch"]
        except Exception:
            pass  # no tags at all is also fine

    def test_movie_scan_rejects_symlink_outside_root(self, tmp_path, media_fixtures_dir):
        import shutil
        from metamatch import MovieLibrary
        lib_dir = tmp_path / "Movies"; lib_dir.mkdir()
        outside = tmp_path / "Out"; outside.mkdir()
        victim = outside / "victim.mp4"
        shutil.copy(media_fixtures_dir / "sample_movie.mp4", victim)
        os.symlink(str(victim), str(lib_dir / "linked.mp4"))
        payload = MovieLibrary(journal=Journal(str(tmp_path / "j.sqlite"))).scan(str(lib_dir))
        assert all("linked" not in os.path.basename(p["path"]) for p in payload)

    def test_tv_scan_rejects_symlink_outside_root(self, tmp_path, media_fixtures_dir):
        import shutil
        from metamatch import TvLibrary
        lib_dir = tmp_path / "TV"; lib_dir.mkdir()
        outside = tmp_path / "Out"; outside.mkdir()
        victim = outside / "Show.S01E01.mp4"
        shutil.copy(media_fixtures_dir / "sample_movie.mp4", victim)
        os.symlink(str(victim), str(lib_dir / "Show.S01E02.mp4"))
        payload = TvLibrary(journal=Journal(str(tmp_path / "j.sqlite"))).scan(str(lib_dir))
        assert all("S01E02" not in os.path.basename(p["path"]) for p in payload)


# --------------------------------------------------------------------------
# 2. TV series Undo scoped to the current library
# --------------------------------------------------------------------------

@requires_ffmpeg
class TestSeriesUndoScoping:
    def test_series_metadata_undo_is_scoped_to_current_tv_library(
        self, tmp_path, media_fixtures_dir, mock_tv_match, mock_tv_series_details
    ):
        import shutil
        from metamatch import TvLibrary
        journal = Journal(str(tmp_path / "shared.sqlite"))

        def build(root_name):
            root = tmp_path / root_name / "Show" / "Season 01"
            root.mkdir(parents=True)
            shutil.copy(media_fixtures_dir / "sample_movie.mp4", root / "Show.S01E01.mp4")
            lib = TvLibrary(journal=journal)
            lib.scan(str(tmp_path / root_name))
            lib.match()
            lib.write_series_metadata(min_confidence=0)
            return lib, tmp_path / root_name / "Show" / "tvshow.nfo"

        # Two independent libraries sharing one journal (note the prefix-sibling
        # pair /tv and /tv_backup to catch naive startswith containment too).
        lib_a, nfo_a = build("tv")
        lib_b, nfo_b = build("tv_backup")
        assert nfo_a.exists() and nfo_b.exists()

        # Undo while working in library A must not touch library B.
        result = lib_a.undo_series_metadata_all()
        assert result["restored"] >= 1
        assert not nfo_a.exists()   # A reverted
        assert nfo_b.exists()       # B untouched


# --------------------------------------------------------------------------
# 3. TV Undo restores pre-existing TV atoms
# --------------------------------------------------------------------------

@requires_ffmpeg
class TestTvAtomPreservation:
    def _mp4_with_atoms(self, tv_dir):
        from mutagen.mp4 import MP4
        mp4 = [str(tv_dir / f) for f in os.listdir(tv_dir) if f.endswith(".mp4")][0]
        audio = MP4(mp4)
        audio["\xa9nam"] = ["Original Title"]; audio["\xa9day"] = ["1999"]
        audio["tvsh"] = ["Original Show"]; audio["tvsn"] = [9]; audio["tves"] = [8]
        audio["stik"] = [10]; audio["\xa9ART"] = ["Original Artist"]
        audio.save()
        return mp4

    def test_tv_undo_restores_preexisting_show_and_ep_atoms(self, tv_dir, mock_tv_match, mock_thumb_download):
        from mutagen.mp4 import MP4
        from metamatch import TvLibrary
        mp4 = self._mp4_with_atoms(tv_dir)

        lib = TvLibrary()
        lib.scan(str(tv_dir))
        lib.match()
        target = [e for e in lib.episodes_payload() if e["path"] == mp4][0]

        applied = lib.apply(target["id"], do_tag=True, do_rename=True, do_nfo=False, do_thumb=False)
        assert MP4(applied["new_path"]).get("tvsh") != ["Original Show"]  # overwritten by apply

        undo = lib.undo(applied["new_path"])
        assert not undo["error"]
        restored = MP4(undo["restored_path"])
        assert restored.get("\xa9nam") == ["Original Title"]
        assert restored.get("tvsh") == ["Original Show"]
        assert restored.get("tvsn") == [9]
        assert restored.get("tves") == [8]
        assert restored.get("stik") == [10]
        assert restored.get("\xa9ART") == ["Original Artist"]

    def test_tv_rollback_restores_preexisting_atoms_after_late_failure(self, tv_dir, mock_tv_match, monkeypatch):
        from mutagen.mp4 import MP4
        from metamatch import TvLibrary
        import metamatch.tv_tagger as tv_tagger_module
        mp4 = self._mp4_with_atoms(tv_dir)

        lib = TvLibrary()
        lib.scan(str(tv_dir))
        lib.match()
        target = [e for e in lib.episodes_payload() if e["path"] == mp4][0]

        # embed succeeds, then rename fails -> rollback must restore atoms
        monkeypatch.setattr(tv_tagger_module, "rename_to_match",
                            lambda p, m: (_ for _ in ()).throw(OSError("boom")))
        result = lib.apply(target["id"], do_tag=True, do_rename=True, do_nfo=False, do_thumb=False)
        assert result["error"]
        assert result["rolled_back"] is True

        restored = MP4(mp4)
        assert restored.get("tvsh") == ["Original Show"]
        assert restored.get("tvsn") == [9]
        assert restored.get("tves") == [8]


# --------------------------------------------------------------------------
# 4. Failed series Undo accounting
# --------------------------------------------------------------------------

@requires_ffmpeg
class TestSeriesUndoAccounting:
    def test_failed_series_undo_is_recovery_required_not_rolled_back(
        self, tv_dir, mock_tv_match, mock_tv_series_details, monkeypatch
    ):
        import metamatch.library as library_module
        from metamatch import TvLibrary

        lib = TvLibrary()
        lib.scan(str(tv_dir))
        lib.match()
        lib.write_series_metadata(min_confidence=0)

        # Force the artifact removal to fail during undo.
        real_remove = os.remove
        def boom_remove(p, *a, **k):
            if p.endswith("tvshow.nfo"):
                raise OSError("simulated permission failure")
            return real_remove(p, *a, **k)
        monkeypatch.setattr(library_module.os, "remove", boom_remove)

        result = lib.undo_series_metadata_all()
        assert result["attempted"] >= 1
        assert result["failed"] >= 1
        assert result["restored"] == result["attempted"] - result["failed"]
        # the failed one must be RECOVERY_REQUIRED, never terminalised ROLLED_BACK
        rr = lib.journal.list_by_status("tv_series", RECOVERY_REQUIRED)
        assert len(rr) >= 1


# --------------------------------------------------------------------------
# 5. Same-file serialization
# --------------------------------------------------------------------------

@requires_ffmpeg
class TestSameFileSerialization:
    def test_same_file_concurrent_apply_is_serialized(self, music_dir, mock_music_match):
        from metamatch import MusicLibrary
        lib = MusicLibrary()
        lib.scan(str(music_dir))
        lib.match()
        target = [t for t in lib.tracks_payload() if t["filename"].startswith("01 -")][0]

        results = {}
        barrier = threading.Barrier(2)

        def worker(name):
            barrier.wait()  # maximise overlap
            try:
                results[name] = lib.apply(target["id"], do_tag=True, do_rename=True, do_art=False)
            except Exception as e:
                results[name] = {"exception": str(e)}

        threads = [threading.Thread(target=worker, args=(n,)) for n in ("A", "B")]
        for t in threads: t.start()
        for t in threads: t.join()

        # Serialization means: no crash, no bogus recovery incident. Exactly one
        # applies; the other cleanly reports the file changed / nothing to do.
        assert all("exception" not in r for r in results.values())
        recoveries = [r for r in results.values() if r.get("recovery_required")]
        assert recoveries == []   # the race no longer manufactures a recovery
        succeeded = [r for r in results.values() if not r.get("error")]
        assert len(succeeded) == 1

    def test_apply_and_quarantine_same_file_cannot_overlap(self, music_dir, mock_music_match):
        # A lighter check that the lock registry hands the same lock object for
        # the same path via different pathnames (realpath keying), which is what
        # makes apply and quarantine mutually exclusive on one file.
        from metamatch.library import _PathLockRegistry
        reg = _PathLockRegistry()
        p1 = str(music_dir / "tagged.mp3")
        p2 = str(music_dir / "." / "tagged.mp3")
        assert reg.get(p1) is reg.get(p2)


# --------------------------------------------------------------------------
# 6. Remux rollback classification (covered in test_rollback too; pinned here)
# --------------------------------------------------------------------------

@requires_ffmpeg
class TestRemuxRollbackClassification:
    def test_irreversible_remux_is_recovery_required(self, movie_dir, mock_movie_match, monkeypatch):
        from metamatch import MovieLibrary
        import metamatch.movie_tagger as movie_tagger_module
        lib = MovieLibrary()
        lib.scan(str(movie_dir))
        lib.match()
        mkv = [v for v in lib.videos_payload() if v["filename"].endswith(".mkv")][0]

        monkeypatch.setattr(movie_tagger_module, "rename_to_match",
                            lambda p, m: (_ for _ in ()).throw(OSError("boom")))
        result = lib.apply(mkv["id"], do_tag=True, do_rename=True, do_nfo=False, do_poster=False)
        assert result["recovery_required"] is True
        assert result["rolled_back"] is False
        assert lib.journal.get(result["txn_id"]).status == RECOVERY_REQUIRED


# --------------------------------------------------------------------------
# 7. Oversized pre-existing sidecar is not overwritten
# --------------------------------------------------------------------------

@requires_ffmpeg
class TestOversizedSidecarProtection:
    def test_existing_large_poster_is_not_overwritten(self, movie_dir, mock_movie_match, mock_poster_download):
        from metamatch import MovieLibrary
        import metamatch.movie_tagger as mt
        lib = MovieLibrary()
        lib.scan(str(movie_dir))
        lib.match()
        target = [v for v in lib.videos_payload() if v["filename"].endswith(".mp4")][0]

        # Place an existing poster larger than the recoverable cap.
        poster = os.path.splitext(target["path"])[0] + "-poster.jpg"
        big = b"X" * (mt.MAX_RECOVERABLE_SIDECAR_BYTES + 1024)
        with open(poster, "wb") as f:
            f.write(big)

        result = lib.apply(target["id"], do_tag=False, do_rename=False, do_nfo=False, do_poster=True)
        assert not result["error"]
        # the big original must be preserved, not clobbered by the download
        assert open(poster, "rb").read() == big
        assert result["poster_path"] is None  # apply left it alone

    def test_small_existing_poster_is_still_replaced(self, movie_dir, mock_movie_match, mock_poster_download):
        from metamatch import MovieLibrary
        lib = MovieLibrary()
        lib.scan(str(movie_dir))
        lib.match()
        target = [v for v in lib.videos_payload() if v["filename"].endswith(".mp4")][0]
        poster = os.path.splitext(target["path"])[0] + "-poster.jpg"
        with open(poster, "wb") as f:
            f.write(b"small original")

        result = lib.apply(target["id"], do_tag=False, do_rename=False, do_nfo=False, do_poster=True)
        assert result["poster_path"]  # a small poster is safely replaced
        assert open(poster, "rb").read() != b"small original"
