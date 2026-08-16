import os

import pytest

from metamatch.scanner import (
    _clean_filename_stem,
    _guess_artist_title_from_filename,
    read_track,
    scan_folder,
)
from conftest import requires_ffmpeg


class TestFilenameParsing:
    def test_strips_official_audio_suffix(self):
        cleaned = _clean_filename_stem("Some Song (Official Audio)")
        assert "official" not in cleaned.lower()
        assert "Some Song" in cleaned

    def test_strips_leading_track_number(self):
        cleaned = _clean_filename_stem("01 - Some Song")
        assert not cleaned.startswith("01")

    def test_underscores_become_spaces(self):
        cleaned = _clean_filename_stem("some_song_title")
        assert "_" not in cleaned
        assert cleaned == "some song title"

    @pytest.mark.parametrize("filename,expected_artist,expected_title", [
        ("Radiohead - Karma Police.mp3", "Radiohead", "Karma Police"),
        ("01 - Test Artist - Test Song (Official Audio).mp3", "Test Artist", "Test Song"),
        ("just_a_title.mp3", None, "just a title"),
    ])
    def test_guess_artist_title(self, filename, expected_artist, expected_title):
        artist, title = _guess_artist_title_from_filename(filename)
        assert artist == expected_artist
        assert title == expected_title


class TestReadTrack:
    @requires_ffmpeg
    def test_reads_real_id3_tags(self, music_dir):
        track = read_track(str(music_dir / "tagged.mp3"))
        assert track.tag_artist == "Radiohead"
        assert track.tag_title == "Karma Police"
        assert track.tag_album == "OK Computer"
        assert track.tag_year == "1997"
        assert track.has_usable_tags is True
        assert track.duration_seconds is not None
        assert track.duration_seconds > 0

    @requires_ffmpeg
    def test_falls_back_to_filename_guess_when_untagged(self, music_dir):
        path = music_dir / "01 - Test Artist - Test Song (Official Audio).mp3"
        track = read_track(str(path))
        assert track.tag_artist is None
        assert track.has_usable_tags is False
        assert track.guess_artist == "Test Artist"
        assert track.guess_title == "Test Song"

    @requires_ffmpeg
    def test_to_dict_includes_match_only_when_set(self, music_dir):
        track = read_track(str(music_dir / "tagged.mp3"))
        assert "match" not in track.to_dict()
        track.match = {"confidence": 90}
        assert track.to_dict()["match"] == {"confidence": 90}


class TestScanFolder:
    @requires_ffmpeg
    def test_finds_all_supported_files(self, music_dir):
        tracks = scan_folder(str(music_dir))
        assert len(tracks) == 2
        filenames = {t.filename for t in tracks}
        assert "tagged.mp3" in filenames

    def test_raises_on_missing_folder(self, tmp_path):
        with pytest.raises(NotADirectoryError):
            scan_folder(str(tmp_path / "does_not_exist"))

    def test_ignores_unsupported_extensions(self, tmp_path):
        (tmp_path / "notes.txt").write_text("hello")
        tracks = scan_folder(str(tmp_path))
        assert tracks == []

    @requires_ffmpeg
    def test_non_recursive_skips_subfolders(self, music_dir):
        sub = music_dir / "subfolder"
        sub.mkdir()
        import shutil
        shutil.copy(music_dir / "tagged.mp3", sub / "tagged.mp3")

        recursive_tracks = scan_folder(str(music_dir), recursive=True)
        flat_tracks = scan_folder(str(music_dir), recursive=False)
        assert len(recursive_tracks) == 3
        assert len(flat_tracks) == 2
