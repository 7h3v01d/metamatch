"""
test_review_023.py
Regression tests for the 0.2.3 adversarial review: a destructive BULK Undo
must never infer "all libraries" from the absence of a current library. After
a restart (self.folder is None), undo_all would otherwise run journal-global
across every library ever recorded in the shared persistent journal. Bulk Undo
now fails closed until a library is explicitly scanned; single-file Undo
remains restart-capable via the per-transaction library_root provenance.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from conftest import requires_ffmpeg
from metamatch.journal import Journal


def _mp3(path, freq=440):
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=1",
                    "-b:a", "128k", path],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)


class TestJournalGlobalListingGuard:
    def test_list_undoable_refuses_global_by_default(self, tmp_path):
        jr = Journal(str(tmp_path / "j.sqlite"))
        with pytest.raises(ValueError):
            jr.list_undoable("music")  # no folder, no opt-in

    def test_list_undoable_allows_global_when_opted_in(self, tmp_path):
        jr = Journal(str(tmp_path / "j.sqlite"))
        assert jr.list_undoable("music", allow_global=True) == []

    def test_get_undoable_paths_is_read_only_global_ok(self, tmp_path):
        jr = Journal(str(tmp_path / "j.sqlite"))
        assert jr.get_undoable_paths("music") == set()   # introspection stays permissive


@requires_ffmpeg
class TestRestartBulkUndoRequiresScope:
    def _seed_two_libraries(self, tmp_path, LibraryCls, make, target_pred):
        jp = str(tmp_path / "j.sqlite")
        roots = []
        for name, freq in (("LibA", 440), ("LibB", 550)):
            d = tmp_path / name
            d.mkdir()
            make(d, freq)
            lib = LibraryCls(journal=Journal(jp))
            payload = lib.scan(str(d))
            lib.match()
            target = [x for x in payload if target_pred(x)][0]
            lib.apply(target["id"], do_tag=True, do_rename=False)
            roots.append(d)
        return jp, roots

    def test_restart_music_undo_all_requires_active_library(self, tmp_path):
        from metamatch import MusicLibrary
        import metamatch.matcher as matcher
        matcher.find_best_match = lambda t: {"recording_id": "r", "release_id": "x",
            "title": "New", "artist": "MetaMatch", "album": "A", "date": "2026-01-01", "confidence": 99}
        jp, _ = self._seed_two_libraries(
            tmp_path, MusicLibrary,
            lambda d, f: _mp3(str(d / "s.mp3"), f),
            lambda x: True)

        restarted = MusicLibrary(journal=Journal(jp))
        assert restarted.folder is None
        with pytest.raises(ValueError):
            restarted.undo_all()

    def test_restart_movie_undo_all_requires_active_library(self, tmp_path, media_fixtures_dir, mock_movie_match):
        import shutil
        from metamatch import MovieLibrary
        def make(d, f):
            shutil.copy(media_fixtures_dir / "sample_movie.mp4", d / "movie.mp4")
        jp, _ = self._seed_two_libraries(tmp_path, MovieLibrary, make, lambda x: x["filename"].endswith(".mp4"))
        restarted = MovieLibrary(journal=Journal(jp))
        with pytest.raises(ValueError):
            restarted.undo_all()

    def test_restart_tv_undo_all_requires_active_library(self, tmp_path, media_fixtures_dir, mock_tv_match, mock_thumb_download):
        import shutil
        from metamatch import TvLibrary
        def make(d, f):
            shutil.copy(media_fixtures_dir / "sample_movie.mp4", d / "Show.S01E01.mp4")
        jp, _ = self._seed_two_libraries(tmp_path, TvLibrary, make, lambda x: x["filename"].endswith(".mp4"))
        restarted = TvLibrary(journal=Journal(jp))
        with pytest.raises(ValueError):
            restarted.undo_all()

    def test_restart_tv_series_undo_all_requires_active_library(self, tmp_path, media_fixtures_dir, mock_tv_match, mock_tv_series_details):
        import shutil
        from metamatch import TvLibrary
        jp = str(tmp_path / "j.sqlite")
        for name in ("TV_A", "TV_B"):
            root = tmp_path / name / "Show" / "Season 01"
            root.mkdir(parents=True)
            shutil.copy(media_fixtures_dir / "sample_movie.mp4", root / "Show.S01E01.mp4")
            lib = TvLibrary(journal=Journal(jp))
            lib.scan(str(tmp_path / name))
            lib.match()
            lib.write_series_metadata(min_confidence=0)

        restarted = TvLibrary(journal=Journal(jp))
        assert restarted.folder is None
        with pytest.raises(ValueError):
            restarted.undo_series_metadata_all()


@requires_ffmpeg
class TestScopedBulkUndoStillWorks:
    def test_scan_A_then_undo_all_leaves_B_untouched(self, tmp_path):
        from metamatch import MusicLibrary
        import metamatch.matcher as matcher
        from mutagen.easyid3 import EasyID3
        matcher.find_best_match = lambda t: {"recording_id": "r", "release_id": "x",
            "title": "New", "artist": "MetaMatch", "album": "A", "date": "2026-01-01", "confidence": 99}
        jp = str(tmp_path / "j.sqlite")

        roots = {}
        for name, freq in (("A", 440), ("B", 550)):
            d = tmp_path / name; d.mkdir()
            _mp3(str(d / "s.mp3"), freq)
            lib = MusicLibrary(journal=Journal(jp))
            p = lib.scan(str(d)); lib.match()
            lib.apply(p[0]["id"], do_tag=True, do_rename=False, do_art=False)
            roots[name] = d

        # restart, scan ONLY A, undo_all
        restarted = MusicLibrary(journal=Journal(jp))
        restarted.scan(str(roots["A"]))
        result = restarted.undo_all()
        assert result["restored"] == 1
        assert EasyID3(str(roots["A"] / "s.mp3")).get("artist") != ["MetaMatch"]   # A reverted
        assert EasyID3(str(roots["B"] / "s.mp3")).get("artist") == ["MetaMatch"]   # B untouched

    def test_restart_single_file_undo_still_allowed(self, tmp_path):
        """Individual Undo remains restart-capable via txn.library_root."""
        from metamatch import MusicLibrary
        import metamatch.matcher as matcher
        from mutagen.easyid3 import EasyID3
        matcher.find_best_match = lambda t: {"recording_id": "r", "release_id": "x",
            "title": "New", "artist": "MetaMatch", "album": "A", "date": "2026-01-01", "confidence": 99}
        jp = str(tmp_path / "j.sqlite")
        d = tmp_path / "Lib"; d.mkdir()
        _mp3(str(d / "s.mp3"))
        lib = MusicLibrary(journal=Journal(jp))
        p = lib.scan(str(d)); lib.match()
        applied = lib.apply(p[0]["id"], do_tag=True, do_rename=False, do_art=False)

        restarted = MusicLibrary(journal=Journal(jp))
        assert restarted.folder is None
        result = restarted.undo(applied["new_path"])   # single-file undo, by name
        assert not result["error"]
        assert EasyID3(str(d / "s.mp3")).get("artist") != ["MetaMatch"]
