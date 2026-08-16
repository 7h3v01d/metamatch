"""
test_library.py
Exercises MusicLibrary and MovieLibrary directly - the whole point of
having them is that they work with no Flask app, no HTTP layer, and no
global state, so these tests instantiate them exactly the way a host
application would: `MusicLibrary()`, call methods, read the results.
"""

import os

import pytest

from metamatch import MusicLibrary, MovieLibrary
from conftest import requires_ffmpeg


@requires_ffmpeg
class TestMusicLibraryBasics:
    def test_two_instances_are_fully_independent(self, music_dir, tmp_path, mock_music_match):
        lib_a = MusicLibrary()
        lib_b = MusicLibrary()

        lib_a.scan(str(music_dir))
        assert len(lib_a.tracks) == 2
        assert len(lib_b.tracks) == 0  # untouched by lib_a's scan

    def test_scan_returns_serializable_payload(self, music_dir):
        lib = MusicLibrary()
        payload = lib.scan(str(music_dir))
        assert isinstance(payload, list)
        assert all("filename" in t and "can_undo" in t for t in payload)

    def test_scan_missing_folder_raises(self, tmp_path):
        lib = MusicLibrary()
        with pytest.raises(NotADirectoryError):
            lib.scan(str(tmp_path / "nope"))


@requires_ffmpeg
class TestMusicLibraryMatchApplyUndo:
    def test_synchronous_match(self, music_dir, mock_music_match):
        lib = MusicLibrary()
        lib.scan(str(music_dir))
        lib.match()  # synchronous - no polling needed
        assert all(t["match"]["confidence"] == 92.5 for t in lib.tracks_payload())

    def test_match_async_then_poll(self, music_dir, mock_music_match):
        import time
        lib = MusicLibrary()
        lib.scan(str(music_dir))
        thread = lib.match_async()
        thread.join(timeout=5)
        assert lib.match_progress_snapshot()["running"] is False
        assert all(t["match"] for t in lib.tracks_payload())

    def test_match_async_raises_without_scan(self):
        lib = MusicLibrary()
        with pytest.raises(ValueError):
            lib.match_async()

    def test_progress_callback_invoked(self, music_dir, mock_music_match):
        lib = MusicLibrary()
        lib.scan(str(music_dir))
        calls = []
        lib.match(progress_callback=lambda done, total: calls.append((done, total)))
        assert calls[-1] == (2, 2)

    def test_apply_then_undo_round_trip(self, music_dir, mock_music_match):
        lib = MusicLibrary()
        lib.scan(str(music_dir))
        lib.match()

        target_id = lib.order[0]
        original_path = target_id
        result = lib.apply(target_id, do_tag=True, do_rename=True)
        assert result["error"] is None
        assert os.path.exists(result["new_path"])

        undo_result = lib.undo(result["new_path"])
        assert undo_result["error"] is None
        assert os.path.exists(original_path)

    def test_apply_unknown_id_raises_keyerror(self, music_dir):
        lib = MusicLibrary()
        lib.scan(str(music_dir))
        with pytest.raises(KeyError):
            lib.apply("/not/a/real/path.mp3")

    def test_apply_without_match_raises_valueerror(self, music_dir):
        lib = MusicLibrary()
        lib.scan(str(music_dir))
        with pytest.raises(ValueError):
            lib.apply(lib.order[0])

    def test_apply_all_and_undo_all(self, music_dir, mock_music_match):
        lib = MusicLibrary()
        lib.scan(str(music_dir))
        lib.match()

        result = lib.apply_all(do_tag=True, do_rename=True, min_confidence=0)
        assert result["succeeded"] == 2

        undo_result = lib.undo_all()
        assert undo_result["restored"] == 2


@requires_ffmpeg
class TestMusicLibraryDuplicates:
    def test_find_and_quarantine(self, music_dir):
        import shutil
        shutil.copy(music_dir / "tagged.mp3", music_dir / "tagged_copy.mp3")

        lib = MusicLibrary()
        lib.scan(str(music_dir))
        dupes = lib.find_duplicates()
        assert len(dupes["exact"]) == 1

        paths = [f["path"] for f in dupes["exact"][0]["files"]]
        result = lib.quarantine([paths[1]])
        assert result["moved"] == 1

    def test_find_duplicates_without_scan_raises(self):
        lib = MusicLibrary()
        with pytest.raises(ValueError):
            lib.find_duplicates()


@requires_ffmpeg
class TestMusicLibraryExport:
    def test_export_csv_returns_string(self, music_dir, mock_music_match):
        lib = MusicLibrary()
        lib.scan(str(music_dir))
        lib.match()
        csv_text = lib.export_csv()
        assert isinstance(csv_text, str)
        assert "Radiohead" in csv_text


@requires_ffmpeg
class TestMovieLibraryBasics:
    def test_two_instances_independent(self, movie_dir):
        lib_a = MovieLibrary()
        lib_b = MovieLibrary()
        lib_a.scan(str(movie_dir))
        assert len(lib_a.videos) == 2
        assert len(lib_b.videos) == 0

    def test_reports_ffmpeg_ffprobe_flags(self):
        lib = MovieLibrary()
        assert isinstance(lib.ffmpeg_available, bool)
        assert isinstance(lib.ffprobe_available, bool)


@requires_ffmpeg
class TestMovieLibraryMatchGating:
    def test_match_async_without_key_raises_tmdb_not_configured(self, movie_dir, isolated_config):
        from metamatch.movie_matcher import TmdbNotConfigured
        lib = MovieLibrary()
        lib.scan(str(movie_dir))
        with pytest.raises(TmdbNotConfigured):
            lib.match_async()

    def test_match_async_without_scan_raises(self, isolated_config):
        isolated_config.set_tmdb_api_key("test-key")
        lib = MovieLibrary()
        with pytest.raises(ValueError):
            lib.match_async()


@requires_ffmpeg
class TestMovieLibraryApplyUndo:
    def test_full_round_trip(self, movie_dir, mock_movie_match, mock_poster_download, isolated_config):
        isolated_config.set_tmdb_api_key("test-key")
        lib = MovieLibrary()
        lib.scan(str(movie_dir))
        lib.match()

        target = [v for v in lib.videos_payload() if v["filename"] == "sample_movie.mp4"][0]
        result = lib.apply(target["id"], do_tag=True, do_rename=True, do_nfo=True, do_poster=True)
        assert result["error"] is None
        assert os.path.exists(result["new_path"])
        assert os.path.exists(result["nfo_path"])
        assert os.path.exists(result["poster_path"])

        undo_result = lib.undo(result["new_path"])
        assert undo_result["error"] is None
        assert os.path.exists(target["id"])

    def test_apply_all_respects_threshold(self, movie_dir, mock_movie_match, isolated_config):
        isolated_config.set_tmdb_api_key("test-key")
        lib = MovieLibrary()
        lib.scan(str(movie_dir))
        lib.match()

        low = lib.apply_all(do_rename=False, do_nfo=False, do_poster=False, min_confidence=99)
        assert low["attempted"] == 0

        high = lib.apply_all(do_rename=False, do_nfo=False, do_poster=False, min_confidence=50)
        assert high["succeeded"] == 2


@requires_ffmpeg
class TestMovieLibraryDuplicates:
    def test_find_duplicates(self, movie_dir):
        import shutil
        shutil.copy(movie_dir / "sample_movie.mp4", movie_dir / "sample_movie_copy.mp4")

        lib = MovieLibrary()
        lib.scan(str(movie_dir))
        dupes = lib.find_duplicates()
        assert len(dupes["exact"]) == 1
