import pytest

from core.movie_matcher import score_candidate, find_best_match, _year_score, TmdbNotConfigured
from core.video_scanner import VideoFile


def make_video(**overrides) -> VideoFile:
    base = dict(
        path="/tmp/fake.mkv", filename="fake.mkv", ext=".mkv", size_bytes=1000,
        duration_seconds=7200.0, tag_title=None, tag_year=None,
        guess_title="The Matrix", guess_year="1999",
    )
    base.update(overrides)
    return VideoFile(**base)


class TestYearScore:
    def test_exact_year_scores_100(self):
        assert _year_score("1999", "1999-03-30") == 100.0

    def test_one_year_off_scores_partial(self):
        assert _year_score("1999", "2000-01-01") == 60.0

    def test_far_off_scores_zero(self):
        assert _year_score("1999", "2020-01-01") == 0.0

    def test_missing_data_returns_none(self):
        assert _year_score(None, "1999-01-01") is None
        assert _year_score("1999", None) is None


class TestScoreCandidate:
    def test_exact_match_scores_highly(self):
        video = make_video()
        candidate = {
            "id": 603, "title": "The Matrix", "original_title": "The Matrix",
            "release_date": "1999-03-30", "overview": "desc", "vote_average": 8.2,
            "poster_path": "/abc.jpg",
        }
        result = score_candidate(video, candidate)
        assert result["confidence"] > 90
        assert result["title"] == "The Matrix"
        assert result["year"] == "1999"
        assert result["poster_url"].endswith("/abc.jpg")
        assert result["tmdb_url"] == "https://www.themoviedb.org/movie/603"

    def test_poor_match_scores_lower(self):
        video = make_video()
        exact = score_candidate(video, {
            "id": 1, "title": "The Matrix", "original_title": "The Matrix",
            "release_date": "1999-03-30", "vote_average": 8.2, "poster_path": None,
        })
        poor = score_candidate(video, {
            "id": 2, "title": "Some Unrelated Film", "original_title": "Some Unrelated Film",
            "release_date": "2015-06-01", "vote_average": 4.0, "poster_path": None,
        })
        assert poor["confidence"] < exact["confidence"]

    def test_checks_original_title_too(self):
        video = make_video(guess_title="Spirited Away", guess_year="2001")
        candidate = {
            "id": 1, "title": "Sen to Chihiro no Kamikakushi",
            "original_title": "Spirited Away", "release_date": "2001-07-20",
            "vote_average": 8.5, "poster_path": None,
        }
        result = score_candidate(video, candidate)
        assert result["title_similarity"] == 100

    def test_no_poster_yields_no_url(self):
        video = make_video()
        result = score_candidate(video, {
            "id": 1, "title": "The Matrix", "release_date": "1999-03-30",
            "vote_average": 8.0, "poster_path": None,
        })
        assert result["poster_url"] is None


class TestFindBestMatch:
    def test_returns_none_without_title(self):
        video = make_video(guess_title=None, tag_title=None)
        assert find_best_match(video) is None

    def test_raises_when_not_configured(self, monkeypatch):
        import core.movie_matcher as movie_matcher_module

        def fake_search(title, year, limit=5):
            raise TmdbNotConfigured("no key")

        monkeypatch.setattr(movie_matcher_module, "_tmdb_search", fake_search)
        video = make_video()
        with pytest.raises(TmdbNotConfigured):
            find_best_match(video)

    def test_picks_best_scoring_candidate(self, monkeypatch):
        import core.movie_matcher as movie_matcher_module

        def fake_search(title, year, limit=5):
            return [
                {"id": 1, "title": "Wrong Movie", "release_date": "2010-01-01", "vote_average": 3.0, "poster_path": None},
                {"id": 2, "title": "The Matrix", "release_date": "1999-03-30", "vote_average": 8.7, "poster_path": None},
            ]

        monkeypatch.setattr(movie_matcher_module, "_tmdb_search", fake_search)
        video = make_video()
        best = find_best_match(video)
        assert best["tmdb_id"] == 2

    def test_retries_without_year_when_no_results(self, monkeypatch):
        import core.movie_matcher as movie_matcher_module
        calls = []

        def fake_search(title, year, limit=5):
            calls.append(year)
            if year:
                return []
            return [{"id": 1, "title": "The Matrix", "release_date": "1999-03-30",
                      "vote_average": 8.0, "poster_path": None}]

        monkeypatch.setattr(movie_matcher_module, "_tmdb_search", fake_search)
        video = make_video()
        best = find_best_match(video)
        assert best is not None
        assert calls == ["1999", None]
