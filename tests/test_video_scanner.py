import pytest

from metamatch.video_scanner import _guess_title_year_from_filename, read_video, scan_folder
from conftest import requires_ffmpeg


class TestGuessTitleYear:
    @pytest.mark.parametrize("filename,expected_title,expected_year", [
        ("The.Matrix.1999.1080p.BluRay.x264-GROUP.mkv", "The Matrix", "1999"),
        ("Inception (2010) [1080p] [BluRay] [5.1] [YIFY].mp4", "Inception", "2010"),
        ("Parasite.2019.KOREAN.1080p.WEBRip.x265-RARBG.mkv", "Parasite", "2019"),
        ("Everything Everywhere All at Once 2022 2160p WEB-DL DDP5.1 HDR HEVC-GROUP.mkv",
         "Everything Everywhere All at Once", "2022"),
        ("spirited_away_2001.mp4", "spirited away", "2001"),
    ])
    def test_realistic_scene_release_names(self, filename, expected_title, expected_year):
        title, year = _guess_title_year_from_filename(filename)
        assert title == expected_title
        assert year == expected_year

    def test_no_year_present(self):
        title, year = _guess_title_year_from_filename("Blade Runner.mkv")
        assert title == "Blade Runner"
        assert year is None

    def test_junk_tokens_stripped(self):
        title, _ = _guess_title_year_from_filename("Movie.Name.1080p.BluRay.x264.AAC5.1-GROUP.mkv")
        for junk in ("1080p", "bluray", "x264", "aac", "5.1"):
            assert junk not in title.lower()


@requires_ffmpeg
class TestReadVideo:
    def test_reads_duration_via_ffprobe(self, movie_dir):
        video = read_video(str(movie_dir / "sample_movie.mp4"))
        assert video.duration_seconds is not None
        assert video.duration_seconds > 0

    def test_parses_scene_release_filename(self, movie_dir):
        path = movie_dir / "Test.Movie.2020.1080p.BluRay.x264-GROUP.mkv"
        video = read_video(str(path))
        assert video.guess_title == "Test Movie"
        assert video.guess_year == "2020"

    def test_to_dict_omits_match_when_unset(self, movie_dir):
        video = read_video(str(movie_dir / "sample_movie.mp4"))
        assert "match" not in video.to_dict()


@requires_ffmpeg
class TestScanVideoFolder:
    def test_finds_supported_extensions(self, movie_dir):
        videos = scan_folder(str(movie_dir))
        assert len(videos) == 2

    def test_ignores_unsupported_extensions(self, tmp_path):
        (tmp_path / "readme.txt").write_text("hi")
        assert scan_folder(str(tmp_path)) == []
