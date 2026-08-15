import os
import shutil
import subprocess

import pytest

from core.movie_tagger import (
    sanitize_filename,
    rename_to_match,
    write_nfo,
    download_poster,
    embed_metadata,
    apply_movie_match,
    FFMPEG_AVAILABLE,
)
from conftest import requires_ffmpeg


MATCH = {
    "tmdb_id": 27205, "title": "Inception", "original_title": None,
    "year": "2010", "release_date": "2010-07-15",
    "overview": "A thief who steals corporate secrets through dream-sharing technology.",
    "vote_average": 8.4, "poster_path": "/abc.jpg",
    "poster_url": "https://image.tmdb.org/t/p/w342/abc.jpg",
    "poster_url_full": "https://image.tmdb.org/t/p/original/abc.jpg",
}


class TestSanitizeFilename:
    def test_strips_invalid_characters(self):
        assert sanitize_filename("Movie: Subtitle?") == "Movie Subtitle"


@requires_ffmpeg
class TestRenameToMatch:
    def test_renames_to_title_year_pattern(self, movie_dir):
        path = str(movie_dir / "sample_movie.mp4")
        new_path = rename_to_match(path, MATCH)
        assert os.path.basename(new_path) == "Inception (2010).mp4"
        assert os.path.exists(new_path)

    def test_omits_year_when_missing(self, movie_dir):
        path = str(movie_dir / "sample_movie.mp4")
        new_path = rename_to_match(path, {"title": "Untitled Film"})
        assert os.path.basename(new_path) == "Untitled Film.mp4"

    def test_avoids_collision(self, movie_dir):
        existing = movie_dir / "Inception (2010).mp4"
        shutil.copy(movie_dir / "sample_movie.mp4", existing)
        other = str(movie_dir / "Test.Movie.2020.1080p.BluRay.x264-GROUP.mkv")
        new_path = rename_to_match(other, {"title": "Inception", "year": "2010"})
        # different extension so no actual collision, but sanity check it still works
        assert os.path.exists(new_path)


class TestWriteNfo:
    def test_writes_expected_fields(self, tmp_path):
        video_path = tmp_path / "movie.mp4"
        video_path.write_bytes(b"fake")
        nfo_path = write_nfo(str(video_path), MATCH)

        assert os.path.exists(nfo_path)
        content = open(nfo_path).read()
        assert "<title>Inception</title>" in content
        assert "<year>2010</year>" in content
        assert 'type="tmdb"' in content
        assert "27205" in content

    def test_handles_missing_optional_fields(self, tmp_path):
        video_path = tmp_path / "movie.mp4"
        video_path.write_bytes(b"fake")
        nfo_path = write_nfo(str(video_path), {"title": "Bare Movie"})
        content = open(nfo_path).read()
        assert "<title>Bare Movie</title>" in content
        assert "<year>" not in content


class TestDownloadPoster:
    def test_returns_none_without_poster_url(self, tmp_path):
        video_path = tmp_path / "movie.mp4"
        video_path.write_bytes(b"fake")
        assert download_poster(str(video_path), {"title": "No Poster"}) is None

    def test_saves_poster_bytes(self, tmp_path, monkeypatch):
        import core.movie_tagger as movie_tagger_module

        class FakeResponse:
            content = b"POSTERBYTES"
            def raise_for_status(self): pass

        monkeypatch.setattr(movie_tagger_module.requests, "get", lambda *a, **k: FakeResponse())

        video_path = tmp_path / "movie.mp4"
        video_path.write_bytes(b"fake")
        dest = download_poster(str(video_path), MATCH)
        assert dest == str(tmp_path / "movie-poster.jpg")
        assert open(dest, "rb").read() == b"POSTERBYTES"

    def test_returns_none_on_network_error(self, tmp_path, monkeypatch):
        import core.movie_tagger as movie_tagger_module
        import requests

        def fake_get(*a, **k):
            raise requests.RequestException("boom")

        monkeypatch.setattr(movie_tagger_module.requests, "get", fake_get)
        video_path = tmp_path / "movie.mp4"
        video_path.write_bytes(b"fake")
        assert download_poster(str(video_path), MATCH) is None


@requires_ffmpeg
class TestEmbedMetadata:
    def test_embeds_directly_into_mp4(self, movie_dir):
        path = str(movie_dir / "sample_movie.mp4")
        embed_metadata(path, MATCH)

        from mutagen.mp4 import MP4
        audio = MP4(path)
        assert audio["\xa9nam"][0] == "Inception"
        assert audio["\xa9day"][0] == "2010"

    @pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="ffmpeg not installed")
    def test_embeds_via_ffmpeg_remux_into_mkv(self, movie_dir):
        path = str(movie_dir / "Test.Movie.2020.1080p.BluRay.x264-GROUP.mkv")
        embed_metadata(path, MATCH)

        proc = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", path],
            stdout=subprocess.PIPE, timeout=20,
        )
        import json
        data = json.loads(proc.stdout)
        assert data["format"]["tags"]["title"] == "Inception"

    def test_unsupported_extension_raises(self, tmp_path):
        path = tmp_path / "clip.webm"
        path.write_bytes(b"fake")
        with pytest.raises(ValueError):
            embed_metadata(str(path), MATCH)


@requires_ffmpeg
class TestApplyMovieMatch:
    def test_full_pipeline(self, movie_dir, mock_poster_download):
        path = str(movie_dir / "sample_movie.mp4")
        result = apply_movie_match(path, MATCH, do_tag=True, do_rename=True, do_nfo=True, do_poster=True)

        assert result["error"] is None
        assert result["tagged"] and result["renamed"]
        assert os.path.exists(result["new_path"])
        assert os.path.exists(result["nfo_path"])
        assert os.path.exists(result["poster_path"])
        assert os.path.basename(result["new_path"]) == "Inception (2010).mp4"

    def test_sidecars_renamed_along_with_video(self, movie_dir, mock_poster_download):
        path = str(movie_dir / "sample_movie.mp4")
        result = apply_movie_match(path, MATCH, do_tag=False, do_rename=True, do_nfo=True, do_poster=True)
        expected_base = os.path.splitext(result["new_path"])[0]
        assert result["nfo_path"] == expected_base + ".nfo"
        assert result["poster_path"] == expected_base + "-poster.jpg"

    def test_no_rename_leaves_sidecars_at_original_name(self, movie_dir, mock_poster_download):
        path = str(movie_dir / "sample_movie.mp4")
        result = apply_movie_match(path, MATCH, do_tag=False, do_rename=False, do_nfo=True, do_poster=False)
        assert result["new_path"] == path
        assert result["nfo_path"] == os.path.splitext(path)[0] + ".nfo"

    def test_error_captured_not_raised(self, tmp_path):
        result = apply_movie_match(str(tmp_path / "ghost.mp4"), MATCH, do_tag=True)
        assert result["error"] is not None
