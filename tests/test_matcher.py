import pytest

from core.matcher import score_candidate, find_best_match, _duration_score
from core.scanner import TrackFile


def make_track(**overrides) -> TrackFile:
    base = dict(
        path="/tmp/fake.mp3", filename="fake.mp3", ext=".mp3", size_bytes=1000,
        duration_seconds=200.0, tag_artist="Radiohead", tag_title="Karma Police",
        tag_album="OK Computer", tag_track_number=None, tag_year="1997",
        guess_artist=None, guess_title=None,
    )
    base.update(overrides)
    return TrackFile(**base)


class TestDurationScore:
    def test_exact_match_scores_100(self):
        assert _duration_score(200.0, 200000) == 100.0

    def test_within_one_second_scores_100(self):
        assert _duration_score(200.5, 200000) == 100.0

    def test_far_off_scores_zero(self):
        assert _duration_score(200.0, 260000) == 0.0

    def test_missing_data_returns_none(self):
        assert _duration_score(None, 200000) is None
        assert _duration_score(200.0, None) is None


class TestScoreCandidate:
    def test_exact_match_scores_highly(self):
        track = make_track()
        candidate = {
            "id": "abc-123", "title": "Karma Police", "score": 100, "length": 200000,
            "artist-credit": [{"name": "Radiohead"}],
            "releases": [{"title": "OK Computer", "date": "1997-05-21",
                          "release-group": {"primary-type": "Album"}}],
        }
        result = score_candidate(track, candidate)
        assert result["confidence"] > 90
        assert result["title"] == "Karma Police"
        assert result["artist"] == "Radiohead"
        assert result["album"] == "OK Computer"
        assert result["release_id"] is None  # this candidate's release dict has no "id"

    def test_release_id_captured_when_present(self):
        track = make_track()
        candidate = {
            "id": "abc-123", "title": "Karma Police", "score": 100, "length": 200000,
            "artist-credit": [{"name": "Radiohead"}],
            "releases": [{"id": "release-xyz", "title": "OK Computer", "date": "1997-05-21",
                          "release-group": {"primary-type": "Album"}}],
        }
        result = score_candidate(track, candidate)
        assert result["release_id"] == "release-xyz"

    def test_poor_match_scores_lower_than_exact(self):
        track = make_track()
        exact = score_candidate(track, {
            "id": "1", "title": "Karma Police", "score": 100, "length": 200000,
            "artist-credit": [{"name": "Radiohead"}], "releases": [],
        })
        poor = score_candidate(track, {
            "id": "2", "title": "Completely Different Song", "score": 20, "length": 400000,
            "artist-credit": [{"name": "Unrelated Artist"}], "releases": [],
        })
        assert poor["confidence"] < exact["confidence"]

    def test_missing_releases_handled_gracefully(self):
        track = make_track()
        result = score_candidate(track, {
            "id": "1", "title": "Karma Police", "score": 90, "length": 200000,
            "artist-credit": [{"name": "Radiohead"}], "releases": [],
        })
        assert result["album"] is None
        assert result["date"] is None

    def test_falls_back_to_filename_guess_when_no_tags(self):
        track = make_track(tag_artist=None, tag_title=None, guess_artist="Radiohead", guess_title="Karma Police")
        result = score_candidate(track, {
            "id": "1", "title": "Karma Police", "score": 90, "length": 200000,
            "artist-credit": [{"name": "Radiohead"}], "releases": [],
        })
        assert result["title_similarity"] > 90


class TestFindBestMatch:
    def test_returns_none_when_no_title_available(self):
        track = make_track(tag_title=None, guess_title=None)
        assert find_best_match(track) is None

    def test_picks_highest_scoring_candidate(self, monkeypatch):
        import core.matcher as matcher_module

        def fake_search(artist, title, limit=5):
            return [
                {"id": "bad", "title": "Wrong Song", "score": 30,
                 "artist-credit": [{"name": "Nobody"}], "releases": []},
                {"id": "good", "title": "Karma Police", "score": 100,
                 "artist-credit": [{"name": "Radiohead"}], "releases": []},
            ]

        monkeypatch.setattr(matcher_module, "_mb_search", fake_search)
        track = make_track()
        best = find_best_match(track)
        assert best["recording_id"] == "good"

    def test_retries_without_artist_when_no_results(self, monkeypatch):
        import core.matcher as matcher_module
        calls = []

        def fake_search(artist, title, limit=5):
            calls.append(artist)
            if artist:
                return []
            return [{"id": "1", "title": "Karma Police", "score": 90,
                      "artist-credit": [{"name": "Radiohead"}], "releases": []}]

        monkeypatch.setattr(matcher_module, "_mb_search", fake_search)
        track = make_track()
        best = find_best_match(track)
        assert best is not None
        assert calls == ["Radiohead", None]
