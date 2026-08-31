"""
test_ambiguity.py
The match-ambiguity / runner-up-margin model: matchers surface how clearly the
winner beat the field, and bulk apply can gate on that margin so a high-
confidence-but-near-tie match isn't auto-applied unattended.
"""

from __future__ import annotations

import pytest

from conftest import requires_ffmpeg
from metamatch import scoring


class TestScoringHelper:
    def test_clear_winner_is_low_ambiguity(self):
        scored = [{"confidence": 95, "title": "A"}, {"confidence": 40, "title": "B"}]
        ru, margin, amb = scoring.summarize_ambiguity(scored, label_fields=("title",))
        assert margin == 55.0
        assert amb == "low"
        assert ru == {"confidence": 40, "title": "B"}

    def test_near_tie_is_high_ambiguity(self):
        scored = [{"confidence": 93, "title": "A"}, {"confidence": 91, "title": "B"}]
        ru, margin, amb = scoring.summarize_ambiguity(scored, label_fields=("title",))
        assert margin == 2.0
        assert amb == "high"

    def test_moderate_band(self):
        scored = [{"confidence": 90}, {"confidence": 82}]
        _, margin, amb = scoring.summarize_ambiguity(scored)
        assert margin == 8.0
        assert amb == "moderate"

    def test_lone_candidate_is_not_ambiguous(self):
        ru, margin, amb = scoring.summarize_ambiguity([{"confidence": 88}])
        assert ru is None and margin is None and amb == "none"

    def test_annotate_writes_onto_winner(self):
        scored = [{"confidence": 93, "title": "A"}, {"confidence": 60, "title": "B"}]
        winner = scoring.annotate_winner(scored, dict(scored[0]), label_fields=("title",))
        assert winner["margin"] == 33.0
        assert winner["ambiguity"] == "low"
        assert winner["runner_up"]["title"] == "B"

    def test_alternate_confidence_key(self):
        scored = [{"series_confidence": 80, "series_name": "X"},
                  {"series_confidence": 79, "series_name": "Y"}]
        ru, margin, amb = scoring.summarize_ambiguity(
            scored, confidence_key="series_confidence", label_fields=("series_name",))
        assert margin == 1.0 and amb == "high" and ru["series_name"] == "Y"


class TestApplyMarginGate:
    """apply_all(min_margin=...) must hold back a high-confidence near-tie
    while still applying a clear winner of the same confidence."""

    def _seed(self, tmp_path, matches):
        import os, subprocess
        from metamatch import MusicLibrary
        from metamatch.journal import Journal
        import metamatch.matcher as matcher

        d = tmp_path / "music"; d.mkdir()
        for i in range(len(matches)):
            subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency={440+i*50}:duration=1",
                            "-b:a", "128k", str(d / f"0{i+1} - t.mp3")],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        seq = iter(matches)
        matcher.find_best_match = lambda t: next(seq)
        lib = MusicLibrary(journal=Journal(str(tmp_path / "j.sqlite")))
        lib.scan(str(d)); lib.match()
        return lib

    def _match(self, conf, margin):
        m = {"recording_id": "r", "release_id": "x", "title": "T", "artist": "A",
             "album": "Al", "date": "2020", "confidence": conf}
        if margin is not None:
            m["margin"] = margin
            m["runner_up"] = {"confidence": conf - margin, "title": "Runner"}
            m["ambiguity"] = "high" if margin < 5 else ("moderate" if margin < 15 else "low")
        else:
            m["margin"] = None
            m["runner_up"] = None
            m["ambiguity"] = "none"
        return m

    @requires_ffmpeg
    def test_margin_zero_applies_both(self, tmp_path):
        lib = self._seed(tmp_path, [self._match(95, 40), self._match(93, 2)])
        r = lib.apply_all(do_tag=True, do_rename=False, min_confidence=80, min_margin=0)
        assert r["attempted"] == 2

    @requires_ffmpeg
    def test_margin_gate_holds_back_near_tie(self, tmp_path):
        lib = self._seed(tmp_path, [self._match(95, 40), self._match(93, 2)])
        r = lib.apply_all(do_tag=True, do_rename=False, min_confidence=80, min_margin=10)
        assert r["attempted"] == 1   # only the 40-pt-margin match

    @requires_ffmpeg
    def test_lone_candidate_passes_margin_gate(self, tmp_path):
        # A match with no runner-up (margin None) is not a near-tie and must
        # still apply even under a margin requirement.
        lib = self._seed(tmp_path, [self._match(90, None)])
        r = lib.apply_all(do_tag=True, do_rename=False, min_confidence=80, min_margin=20)
        assert r["attempted"] == 1

    @requires_ffmpeg
    def test_nan_margin_is_rejected(self, tmp_path):
        lib = self._seed(tmp_path, [self._match(90, 30)])
        with pytest.raises(ValueError):
            lib.apply_all(do_tag=True, min_confidence=80, min_margin=float("nan"))


@requires_ffmpeg
class TestMatchersAnnotate:
    def test_music_match_carries_ambiguity_fields(self, music_dir, mock_music_match):
        from metamatch import MusicLibrary
        lib = MusicLibrary()
        lib.scan(str(music_dir))
        lib.match()
        m = [t for t in lib.tracks_payload() if t["match"]][0]["match"]
        # the mock returns a single candidate, so ambiguity is "none" but the
        # keys must be present and well-formed
        assert "margin" in m and "ambiguity" in m and "runner_up" in m
