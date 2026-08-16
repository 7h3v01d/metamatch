import os
import shutil

import pytest

from metamatch.tagger import (
    sanitize_filename,
    apply_tags,
    rename_to_match,
    embed_cover_art,
    set_or_clear_tags,
    apply_match,
)
from metamatch.scanner import read_track
from conftest import requires_ffmpeg


class TestSanitizeFilename:
    def test_strips_invalid_characters(self):
        assert sanitize_filename('Bad:Name/With*Chars?') == "BadNameWithChars"

    def test_collapses_whitespace(self):
        assert sanitize_filename("Too    Many   Spaces") == "Too Many Spaces"

    def test_empty_result_falls_back(self):
        assert sanitize_filename("???") == "untitled"


@requires_ffmpeg
class TestApplyTags:
    def test_writes_id3_tags_to_mp3(self, music_dir):
        path = str(music_dir / "01 - Test Artist - Test Song (Official Audio).mp3")
        match = {"artist": "Radiohead", "title": "Karma Police", "album": "OK Computer", "date": "1997-05-21"}
        apply_tags(path, match)

        reread = read_track(path)
        assert reread.tag_artist == "Radiohead"
        assert reread.tag_title == "Karma Police"
        assert reread.tag_album == "OK Computer"
        assert reread.tag_year == "1997"

    def test_writes_asf_tags_to_wma(self, media_fixtures_dir, tmp_path):
        path = tmp_path / "test.wma"
        shutil.copy(media_fixtures_dir / "sample.wma", path)
        match = {"artist": "Test Artist", "title": "Test Title", "album": "Test Album", "date": "2020"}
        apply_tags(str(path), match)

        reread = read_track(str(path))
        assert reread.tag_artist == "Test Artist"
        assert reread.tag_title == "Test Title"

    def test_only_sets_provided_fields(self, music_dir):
        path = str(music_dir / "tagged.mp3")
        apply_tags(path, {"artist": "New Artist"})
        reread = read_track(path)
        assert reread.tag_artist == "New Artist"
        assert reread.tag_title == "Karma Police"  # untouched


@requires_ffmpeg
class TestRenameToMatch:
    def test_renames_to_artist_dash_title(self, music_dir):
        path = str(music_dir / "tagged.mp3")
        new_path = rename_to_match(path, {"artist": "Radiohead", "title": "Karma Police"})
        assert os.path.basename(new_path) == "Radiohead - Karma Police.mp3"
        assert os.path.exists(new_path)
        assert not os.path.exists(path)

    def test_avoids_collision_with_existing_file(self, music_dir):
        target = music_dir / "Radiohead - Karma Police.mp3"
        shutil.copy(music_dir / "01 - Test Artist - Test Song (Official Audio).mp3", target)

        path = str(music_dir / "tagged.mp3")
        new_path = rename_to_match(path, {"artist": "Radiohead", "title": "Karma Police"})
        assert new_path != str(target)
        assert "(2)" in os.path.basename(new_path)

    def test_missing_fields_use_placeholders(self, music_dir):
        path = str(music_dir / "tagged.mp3")
        new_path = rename_to_match(path, {})
        assert "Unknown Artist" in new_path
        assert "Unknown Title" in new_path


@requires_ffmpeg
class TestEmbedCoverArt:
    FAKE_JPEG = b"\xff\xd8\xff\xe0FAKEJPEGDATA" * 5
    FAKE_PNG = b"\x89PNG\r\n\x1a\nFAKEPNGDATA" * 5

    def test_embeds_into_mp3(self, music_dir):
        path = str(music_dir / "tagged.mp3")
        embed_cover_art(path, self.FAKE_JPEG, "image/jpeg")

        from mutagen.id3 import ID3
        tags = ID3(path)
        apics = tags.getall("APIC")
        assert len(apics) == 1
        assert apics[0].data == self.FAKE_JPEG

    def test_embeds_into_wma(self, media_fixtures_dir, tmp_path):
        path = tmp_path / "test.wma"
        shutil.copy(media_fixtures_dir / "sample.wma", path)
        embed_cover_art(str(path), self.FAKE_JPEG, "image/jpeg")

        from mutagen.asf import ASF
        audio = ASF(str(path))
        raw = bytes(audio["WM/Picture"][0].value)
        assert raw.endswith(self.FAKE_JPEG)

    def test_embeds_into_flac(self, media_fixtures_dir, tmp_path):
        path = tmp_path / "test.flac"
        shutil.copy(media_fixtures_dir / "sample.flac", path)
        embed_cover_art(str(path), self.FAKE_PNG, "image/png")

        from mutagen.flac import FLAC
        audio = FLAC(str(path))
        assert len(audio.pictures) == 1
        assert audio.pictures[0].data == self.FAKE_PNG

    def test_replaces_existing_art_rather_than_stacking(self, music_dir):
        path = str(music_dir / "tagged.mp3")
        embed_cover_art(path, self.FAKE_JPEG, "image/jpeg")
        embed_cover_art(path, self.FAKE_PNG, "image/png")

        from mutagen.id3 import ID3
        tags = ID3(path)
        apics = tags.getall("APIC")
        assert len(apics) == 1
        assert apics[0].data == self.FAKE_PNG

    def test_unsupported_extension_raises(self, tmp_path):
        path = tmp_path / "video.mp4"
        path.write_bytes(b"not real")
        with pytest.raises(ValueError):
            embed_cover_art(str(path), self.FAKE_JPEG, "image/jpeg")


@requires_ffmpeg
class TestSetOrClearTags:
    def test_clears_field_when_none(self, music_dir):
        path = str(music_dir / "tagged.mp3")
        set_or_clear_tags(path, artist="Radiohead", title=None, album="OK Computer", date="1997")
        reread = read_track(path)
        assert reread.tag_artist == "Radiohead"
        assert reread.tag_title is None

    def test_restores_all_original_values(self, music_dir):
        path = str(music_dir / "tagged.mp3")
        apply_tags(path, {"artist": "Someone Else", "title": "Different Song"})
        set_or_clear_tags(path, artist="Radiohead", title="Karma Police", album="OK Computer", date="1997")
        reread = read_track(path)
        assert reread.tag_artist == "Radiohead"
        assert reread.tag_title == "Karma Police"


@requires_ffmpeg
class TestApplyMatch:
    def test_full_pipeline_tag_and_rename(self, music_dir):
        path = str(music_dir / "01 - Test Artist - Test Song (Official Audio).mp3")
        match = {"artist": "Radiohead", "title": "Karma Police", "album": "OK Computer", "date": "1997"}
        result = apply_match(path, match, do_tag=True, do_rename=True)

        assert result["error"] is None
        assert result["tagged"] is True
        assert result["renamed"] is True
        assert os.path.exists(result["new_path"])

    def test_art_only_applied_when_bytes_given(self, music_dir):
        path = str(music_dir / "tagged.mp3")
        result = apply_match(path, {"artist": "X", "title": "Y"}, do_tag=False, do_rename=False,
                              do_art=True, art_bytes=None)
        assert result["art_embedded"] is False

    def test_error_captured_not_raised(self, tmp_path):
        # A path that doesn't exist should produce a captured error, not an exception.
        result = apply_match(str(tmp_path / "missing.mp3"), {"artist": "X", "title": "Y"},
                              do_tag=True, do_rename=False)
        assert result["error"] is not None
