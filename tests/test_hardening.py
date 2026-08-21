"""
test_hardening.py
Regression tests for issues found in an adversarial review and fixed
afterward. Each test class is named after the specific failure mode it
guards against, so a future regression here should be immediately
recognizable as "the thing that adversarial review caught."
"""

import os
import subprocess

import pytest

from conftest import requires_ffmpeg


@requires_ffmpeg
class TestQuarantineAuthority:
    """quarantine() must refuse any path that isn't a file this scan itself discovered."""

    def test_music_refuses_untracked_file(self, tmp_path):
        from metamatch import MusicLibrary

        music_dir = tmp_path / "music"
        music_dir.mkdir()
        important = tmp_path / "IMPORTANT.txt"
        important.write_text("do not touch")

        lib = MusicLibrary()
        lib.scan(str(music_dir))  # empty - important.txt isn't audio and isn't in this folder anyway
        result = lib.quarantine([str(important)])

        assert result["moved"] == 0
        assert important.exists()
        assert result["results"][0]["error"] is not None

    def test_music_refuses_path_outside_scan_even_if_media_extension(self, music_dir, tmp_path):
        from metamatch import MusicLibrary
        import shutil

        other_dir = tmp_path / "other_music"
        other_dir.mkdir()
        shutil.copy(music_dir / "tagged.mp3", other_dir / "unrelated.mp3")

        lib = MusicLibrary()
        lib.scan(str(music_dir))
        result = lib.quarantine([str(other_dir / "unrelated.mp3")])

        assert result["moved"] == 0
        assert (other_dir / "unrelated.mp3").exists()

    def test_movie_refuses_untracked_file(self, tmp_path):
        from metamatch import MovieLibrary

        movie_dir = tmp_path / "movies"
        movie_dir.mkdir()
        important = tmp_path / "IMPORTANT.txt"
        important.write_text("do not touch")

        lib = MovieLibrary()
        lib.scan(str(movie_dir))
        result = lib.quarantine([str(important)])

        assert result["moved"] == 0
        assert important.exists()

    def test_accepts_files_actually_in_the_scan(self, music_dir):
        from metamatch import MusicLibrary

        lib = MusicLibrary()
        lib.scan(str(music_dir))
        target = lib.order[0]
        result = lib.quarantine([target])
        assert result["moved"] == 1
        assert not os.path.exists(target)


def _stream_types(path: str) -> list:
    proc = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "stream=codec_type,codec_name", "-of", "csv=p=0", path],
        stdout=subprocess.PIPE, timeout=20,
    )
    return sorted(proc.stdout.decode().strip().split("\n"))


@requires_ffmpeg
class TestRemuxStreamPreservation:
    """Embedding metadata into mkv/avi/mov/wmv must not silently drop streams."""

    def test_multi_audio_track_file_keeps_all_streams(self, tmp_path):
        multi = tmp_path / "multi_audio.mkv"
        subprocess.run([
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=blue:s=160x120:d=1",
            "-f", "lavfi", "-i", "sine=frequency=440:d=1",
            "-f", "lavfi", "-i", "sine=frequency=220:d=1",
            "-map", "0:v", "-map", "1:a", "-map", "2:a",
            "-c:v", "libx264", "-c:a", "aac", "-pix_fmt", "yuv420p", str(multi),
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=30)

        before = _stream_types(str(multi))
        assert len(before) == 3  # sanity-check the fixture itself has 3 streams

        from metamatch.movie_tagger import embed_metadata
        embed_metadata(str(multi), {"title": "Test", "year": "2020"})

        after = _stream_types(str(multi))
        assert before == after, "remux must preserve every stream, not just the auto-selected ones"

    def test_metadata_still_applied_after_safe_remux(self, tmp_path):
        video = tmp_path / "movie.mkv"
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=160x120:d=1",
                         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=30)

        from metamatch.movie_tagger import embed_metadata
        embed_metadata(str(video), {"title": "Real Title", "year": "2021"})

        proc = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(video)],
                               stdout=subprocess.PIPE, timeout=20)
        import json
        tags = json.loads(proc.stdout)["format"].get("tags", {})
        assert tags.get("title") == "Real Title"


@requires_ffmpeg
class TestUndoBaselinePreservedThroughRepeatedApply:
    def test_double_apply_then_undo_restores_true_original(self, music_dir, mock_music_match):
        from metamatch import MusicLibrary

        lib = MusicLibrary()
        lib.scan(str(music_dir))
        lib.match()

        target = [t for t in lib.tracks_payload() if t["tag_artist"] == "Radiohead"][0]
        original_path = target["id"]

        r1 = lib.apply(target["id"], do_tag=True, do_rename=True)
        r2 = lib.apply(r1["new_path"], do_tag=True, do_rename=True)  # accidental double apply

        record = lib.journal.get_active_for_path("music", r2["new_path"])
        assert record.original_path == original_path

        undo_result = lib.undo(r2["new_path"])
        assert undo_result["error"] is None
        assert os.path.exists(original_path)

    def test_triple_apply_still_preserves_baseline(self, music_dir, mock_music_match):
        from metamatch import MusicLibrary

        lib = MusicLibrary()
        lib.scan(str(music_dir))
        lib.match()

        target = [t for t in lib.tracks_payload() if t["tag_artist"] == "Radiohead"][0]
        original_path = target["id"]

        r1 = lib.apply(target["id"], do_tag=True, do_rename=True)
        r2 = lib.apply(r1["new_path"], do_tag=True, do_rename=True)
        r3 = lib.apply(r2["new_path"], do_tag=True, do_rename=True)

        assert lib.journal.get_active_for_path("music", r3["new_path"]).original_path == original_path


@requires_ffmpeg
class TestSidecarRenameCollisionSafety:
    def test_does_not_clobber_preexisting_sidecar(self, movie_dir, mock_poster_download):
        from metamatch.movie_tagger import apply_movie_match

        # Plant a pre-existing, unrelated file at the name the rename will collide with.
        (movie_dir / "Film (2020).nfo").write_text("PRE-EXISTING VALUABLE CONTENT")
        (movie_dir / "Film (2020).mp4").write_bytes(b"unrelated other movie")

        match = {"title": "Film", "year": "2020"}
        result = apply_movie_match(str(movie_dir / "sample_movie.mp4"), match, do_rename=True, do_nfo=True)

        assert (movie_dir / "Film (2020).nfo").read_text() == "PRE-EXISTING VALUABLE CONTENT"
        assert os.path.exists(result["nfo_path"])
        assert result["nfo_path"] != str(movie_dir / "Film (2020).nfo")


@requires_ffmpeg
class TestConfidenceThresholdValidation:
    def test_nan_threshold_rejected(self, music_dir, mock_music_match):
        from metamatch import MusicLibrary

        lib = MusicLibrary()
        lib.scan(str(music_dir))
        lib.match()
        with pytest.raises(ValueError):
            lib.apply_all(min_confidence=float("nan"))

    def test_infinite_threshold_rejected(self, music_dir):
        from metamatch import MusicLibrary
        lib = MusicLibrary()
        lib.scan(str(music_dir))
        with pytest.raises(ValueError):
            lib.apply_all(min_confidence=float("inf"))

    def test_out_of_range_threshold_rejected(self, music_dir):
        from metamatch import MusicLibrary
        lib = MusicLibrary()
        lib.scan(str(music_dir))
        with pytest.raises(ValueError):
            lib.apply_all(min_confidence=150)
        with pytest.raises(ValueError):
            lib.apply_all(min_confidence=-1)

    def test_nan_via_flask_route_returns_400(self, app_client, music_dir, mock_music_match, wait_for_progress):
        app_client.post("/api/scan", json={"folder": str(music_dir)})
        app_client.post("/api/match/start")
        wait_for_progress(app_client, "/api/match/progress")

        resp = app_client.post("/api/apply_all", json={"min_confidence": float("nan")})
        assert resp.status_code == 400


@requires_ffmpeg
class TestBulkApplyReportsFailuresHonestly:
    def test_response_has_attempted_succeeded_failed(self, music_dir, mock_music_match):
        from metamatch import MusicLibrary

        lib = MusicLibrary()
        lib.scan(str(music_dir))
        lib.match()
        result = lib.apply_all(min_confidence=0)

        assert "attempted" in result
        assert "succeeded" in result
        assert "failed" in result
        assert result["attempted"] == result["succeeded"] + result["failed"]

    def test_old_misleading_applied_key_is_gone(self, music_dir, mock_music_match):
        from metamatch import MusicLibrary

        lib = MusicLibrary()
        lib.scan(str(music_dir))
        lib.match()
        result = lib.apply_all(min_confidence=0)
        assert "applied" not in result


@requires_ffmpeg
class TestMatchProgressRecoversFromUnexpectedException:
    def test_movie_match_does_not_get_stuck_running(self, movie_dir, isolated_config, monkeypatch):
        isolated_config.set_tmdb_api_key("fake-key")
        import metamatch.movie_matcher as movie_matcher_module

        def broken_find(video):
            raise RuntimeError("simulated unexpected crash")

        monkeypatch.setattr(movie_matcher_module, "find_best_match", broken_find)

        from metamatch import MovieLibrary
        lib = MovieLibrary()
        lib.scan(str(movie_dir))
        lib.match()  # must not raise past this point, and must clear "running"

        progress = lib.match_progress_snapshot()
        assert progress["running"] is False
        assert progress["error"] is not None

    def test_music_match_isolates_per_track_failures(self, music_dir, monkeypatch):
        import metamatch.matcher as matcher_module

        call_count = {"n": 0}

        def flaky_find(track):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated failure on first track")
            return {"recording_id": "ok", "release_id": None, "title": "T", "artist": "A",
                     "album": None, "date": None, "length_ms": None, "mb_score": 90,
                     "title_similarity": 90, "artist_similarity": 90, "duration_similarity": None,
                     "confidence": 90.0, "musicbrainz_url": "x"}

        monkeypatch.setattr(matcher_module, "find_best_match", flaky_find)

        from metamatch import MusicLibrary
        lib = MusicLibrary()
        lib.scan(str(music_dir))
        lib.match()  # must not raise even though one track's matcher call blew up

        payload = lib.tracks_payload()
        assert any(t.get("match") is None for t in payload)  # the one that failed
        assert any(t.get("match") is not None for t in payload)  # the rest still matched


@requires_ffmpeg
class TestMatchAsyncAtomicStart:
    def _slow_matcher(self, monkeypatch):
        """A mocked matcher slow enough to reliably observe the in-progress
        state - with the normal fast mock, the background thread can finish
        before the test ever checks it, making these assertions meaningless."""
        import time
        import metamatch.matcher as matcher_module

        def slow_find(track):
            time.sleep(0.3)
            return {
                "recording_id": "r1", "release_id": None, "title": "T", "artist": "A",
                "album": None, "date": None, "length_ms": None, "mb_score": 90,
                "title_similarity": 90, "artist_similarity": 90, "duration_similarity": None,
                "confidence": 90.0, "musicbrainz_url": "x",
            }

        monkeypatch.setattr(matcher_module, "find_best_match", slow_find)

    def test_running_flag_set_before_thread_start_returns(self, music_dir, monkeypatch):
        """match_async() must claim 'running' atomically with its own check,
        so two near-simultaneous callers can't both slip past and start
        two matching threads."""
        self._slow_matcher(monkeypatch)
        from metamatch import MusicLibrary

        lib = MusicLibrary()
        lib.scan(str(music_dir))
        thread = lib.match_async()
        # By the time match_async() has returned, running must already be
        # True - not "eventually true once the thread gets scheduled."
        assert lib.match_progress_snapshot()["running"] is True
        thread.join(timeout=5)

    def test_second_concurrent_call_is_rejected(self, music_dir, monkeypatch):
        self._slow_matcher(monkeypatch)
        from metamatch import MusicLibrary

        lib = MusicLibrary()
        lib.scan(str(music_dir))
        thread = lib.match_async()
        with pytest.raises(RuntimeError):
            lib.match_async()
        thread.join(timeout=5)


@requires_ffmpeg
class TestQuarantineDirExcludedFromScan:
    def test_music_quarantined_file_not_rescanned(self, music_dir):
        from metamatch import MusicLibrary

        lib = MusicLibrary()
        lib.scan(str(music_dir))
        lib.quarantine([lib.order[0]])

        lib2 = MusicLibrary()
        lib2.scan(str(music_dir))
        assert all("_metamatch_duplicates" not in p for p in lib2.order)

    def test_movie_quarantined_file_not_rescanned(self, movie_dir):
        from metamatch import MovieLibrary

        lib = MovieLibrary()
        lib.scan(str(movie_dir))
        lib.quarantine([lib.order[0]])

        lib2 = MovieLibrary()
        lib2.scan(str(movie_dir))
        assert all("_metamatch_duplicates" not in p for p in lib2.order)


@requires_ffmpeg
class TestDedupNoDoubleListing:
    def test_byte_identical_pair_not_also_probable(self, music_dir):
        import shutil
        from metamatch import MusicLibrary

        shutil.copy(music_dir / "tagged.mp3", music_dir / "tagged_copy.mp3")

        lib = MusicLibrary()
        lib.scan(str(music_dir))
        dupes = lib.find_duplicates()

        assert len(dupes["exact"]) == 1
        assert len(dupes["probable"]) == 0

    def test_different_encodes_of_same_song_still_show_as_probable(self, music_dir, mock_music_match):
        """Sanity check the fix didn't overcorrect: genuinely different files
        that share a match should still surface as probable duplicates."""
        from metamatch import MusicLibrary

        lib = MusicLibrary()
        lib.scan(str(music_dir))  # tagged.mp3 + the messy-named mp3 are NOT byte-identical
        lib.match()  # both get the same mocked match (same recording_id)

        dupes = lib.find_duplicates()
        assert len(dupes["probable"]) == 1


class TestCsvFormulaInjectionDefense:
    def test_formula_prefixes_are_neutralized(self):
        from metamatch.library import _csv_safe

        for dangerous in ("=1+1", "+SUM(A1)", "-2+3", "@cmd"):
            safe = _csv_safe(dangerous)
            assert safe.startswith("'")
            assert safe[1:] == dangerous

    def test_normal_text_untouched(self):
        from metamatch.library import _csv_safe
        assert _csv_safe("Radiohead") == "Radiohead"
        assert _csv_safe(92.5) == 92.5

    @requires_ffmpeg
    def test_export_csv_neutralizes_malicious_matched_title(self, music_dir, monkeypatch):
        import metamatch.matcher as matcher_module
        from metamatch import MusicLibrary

        def fake_find(track):
            return {"recording_id": "r", "release_id": None, "title": "=cmd|'/c calc'!A1",
                     "artist": "A", "album": None, "date": None, "length_ms": None, "mb_score": 90,
                     "title_similarity": 90, "artist_similarity": 90, "duration_similarity": None,
                     "confidence": 90.0, "musicbrainz_url": "x"}

        monkeypatch.setattr(matcher_module, "find_best_match", fake_find)

        lib = MusicLibrary()
        lib.scan(str(music_dir))
        lib.match()
        csv_text = lib.export_csv()
        assert "\n=cmd" not in csv_text
        assert "'=cmd" in csv_text


class TestFilenameSanitizationHardening:
    def test_reserved_windows_names_prefixed(self):
        from metamatch.tagger import sanitize_filename as music_sanitize
        from metamatch.movie_tagger import sanitize_filename as movie_sanitize

        for name in ("CON", "con", "NUL", "COM1", "LPT9", "Aux"):
            assert music_sanitize(name).upper() != name.upper()
            assert movie_sanitize(name).upper() != name.upper()

    def test_long_names_truncated(self):
        from metamatch.tagger import sanitize_filename
        result = sanitize_filename("A" * 500)
        assert len(result) <= 200

    def test_normal_names_untouched(self):
        from metamatch.tagger import sanitize_filename
        assert sanitize_filename("Radiohead - Karma Police") == "Radiohead - Karma Police"


@requires_ffmpeg
class TestWavSupport:
    def test_read_write_roundtrip(self, tmp_path):
        wav_path = tmp_path / "test.wav"
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(wav_path)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=30)

        from metamatch.tagger import apply_tags
        from metamatch.scanner import read_track

        apply_tags(str(wav_path), {"artist": "WAV Artist", "title": "WAV Title", "album": "WAV Album", "date": "2020"})
        track = read_track(str(wav_path))
        assert track.tag_artist == "WAV Artist"
        assert track.tag_title == "WAV Title"

    def test_cover_art_embed(self, tmp_path):
        wav_path = tmp_path / "test.wav"
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(wav_path)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=30)

        from metamatch.tagger import embed_cover_art
        fake_jpeg = b"\xff\xd8\xff\xe0FAKEJPEGDATA" * 5
        embed_cover_art(str(wav_path), fake_jpeg, "image/jpeg")

        from mutagen.wave import WAVE
        audio = WAVE(str(wav_path))
        apics = audio.tags.getall("APIC")
        assert len(apics) == 1
        assert apics[0].data == fake_jpeg

    def test_undo_clears_fields_correctly(self, tmp_path):
        wav_path = tmp_path / "test.wav"
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(wav_path)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=30)

        from metamatch.tagger import apply_tags, set_or_clear_tags
        from metamatch.scanner import read_track

        apply_tags(str(wav_path), {"artist": "Someone", "title": "Something"})
        set_or_clear_tags(str(wav_path), artist="Restored", title=None, album=None, date=None)

        track = read_track(str(wav_path))
        assert track.tag_artist == "Restored"
        assert track.tag_title is None


@requires_ffmpeg
class TestSidecarContentSnapshotting:
    """Undo should restore a pre-existing sidecar's actual original bytes,
    not just leave *some* file at that path."""

    def test_preexisting_nfo_content_actually_restored(self, movie_dir, mock_movie_match, isolated_config):
        isolated_config.set_tmdb_api_key("test-key")
        nfo_path = str(movie_dir / "sample_movie.nfo")
        with open(nfo_path, "w") as f:
            f.write("ORIGINAL USER CONTENT")

        from metamatch import MovieLibrary
        lib = MovieLibrary()
        lib.scan(str(movie_dir))
        lib.match()

        target = [v for v in lib.videos_payload() if v["filename"] == "sample_movie.mp4"][0]
        apply_result = lib.apply(target["id"], do_tag=False, do_rename=False, do_nfo=True, do_poster=False)
        with open(nfo_path) as f:
            assert f.read() != "ORIGINAL USER CONTENT"  # confirm it really got overwritten

        lib.undo(apply_result["new_path"])
        with open(nfo_path) as f:
            assert f.read() == "ORIGINAL USER CONTENT"

    def test_oversized_sidecar_falls_back_to_leaving_it_alone(self, movie_dir, mock_movie_match, isolated_config):
        """A sidecar larger than the snapshot cap can't have its exact bytes
        restored - undo should leave it in place rather than guess, which is
        the same safe fallback the pre-fix code always used."""
        isolated_config.set_tmdb_api_key("test-key")
        from metamatch import library as lib_mod

        nfo_path = str(movie_dir / "sample_movie.nfo")
        with open(nfo_path, "w") as f:
            f.write("X" * 100)

        old_cap = lib_mod._MAX_SIDECAR_SNAPSHOT_BYTES
        lib_mod._MAX_SIDECAR_SNAPSHOT_BYTES = 10  # force this 100-byte file to exceed the cap
        try:
            lib = lib_mod.MovieLibrary()
            lib.scan(str(movie_dir))
            lib.match()
            target = [v for v in lib.videos_payload() if v["filename"] == "sample_movie.mp4"][0]
            apply_result = lib.apply(target["id"], do_tag=False, do_rename=False, do_nfo=True, do_poster=False)
            lib.undo(apply_result["new_path"])
            assert os.path.exists(nfo_path)  # not deleted, even though we couldn't restore exact content
        finally:
            lib_mod._MAX_SIDECAR_SNAPSHOT_BYTES = old_cap


@requires_ffmpeg
class TestMovieConfidenceDoesNotUsePopularityAsIdentity:
    """vote_average (a movie's rating) is not evidence of *which* movie a
    file is - a popular movie sharing a title with the correct-but-obscure
    one shouldn't be able to win purely by having a higher rating."""

    def test_first_ranked_search_result_wins_over_higher_rated_decoy(self):
        import metamatch.movie_matcher as movie_matcher_module
        from metamatch.video_scanner import VideoFile

        video = VideoFile(path="/tmp/x.mkv", filename="x.mkv", ext=".mkv", size_bytes=1,
                           duration_seconds=None, tag_title=None, tag_year=None,
                           guess_title="Alien", guess_year=None)

        def fake_search(title, year, limit=5):
            return [
                {"id": 1, "title": "Alien", "release_date": "1979-05-25",
                 "vote_average": 6.5, "poster_path": None},  # correct, TMDB ranks it first
                {"id": 2, "title": "Alien", "release_date": "1996-01-01",
                 "vote_average": 9.5, "poster_path": None},  # unrelated, higher-rated, ranked second
            ]

        original = movie_matcher_module._tmdb_search
        movie_matcher_module._tmdb_search = fake_search
        try:
            best = movie_matcher_module.find_best_match(video)
            assert best["tmdb_id"] == 1
        finally:
            movie_matcher_module._tmdb_search = original

    def test_vote_average_still_returned_for_display_but_unused_in_score(self):
        from metamatch.movie_matcher import score_candidate
        from metamatch.video_scanner import VideoFile

        video = VideoFile(path="/tmp/x.mkv", filename="x.mkv", ext=".mkv", size_bytes=1,
                           duration_seconds=None, tag_title=None, tag_year=None,
                           guess_title="Alien", guess_year="1979")

        low_rating = score_candidate(video, {"id": 1, "title": "Alien", "release_date": "1979-05-25",
                                              "vote_average": 1.0, "poster_path": None}, rank=0)
        high_rating = score_candidate(video, {"id": 2, "title": "Alien", "release_date": "1979-05-25",
                                               "vote_average": 9.9, "poster_path": None}, rank=0)
        assert low_rating["confidence"] == high_rating["confidence"]
        assert low_rating["vote_average"] == 1.0  # still present for the UI to show


@requires_ffmpeg
class TestArtCacheHardening:
    """The Cover Art Archive cache must be bounded (no unbounded memory
    growth for a large library) and must not cache a failure forever
    (a transient network hiccup shouldn't permanently block retrying)."""

    def setup_method(self):
        from metamatch import art
        art._cache.clear()
        art._negative_cache.clear()

    def test_cache_is_bounded(self, monkeypatch):
        from metamatch import art

        def fake_get(url, **kwargs):
            class FakeResp:
                status_code = 200
                content = b"IMG"
                headers = {"Content-Type": "image/jpeg"}
            return FakeResp()

        monkeypatch.setattr(art.requests, "get", fake_get)
        old_max = art._MAX_CACHE_ENTRIES
        art._MAX_CACHE_ENTRIES = 5
        try:
            for i in range(20):
                art.fetch_cover_art(f"release-{i}")
            assert len(art._cache) <= 5
        finally:
            art._MAX_CACHE_ENTRIES = old_max

    def test_failure_is_not_cached_forever(self, monkeypatch):
        from metamatch import art
        import requests

        call_count = {"n": 0}

        def flaky_get(url, **kwargs):
            call_count["n"] += 1
            raise requests.RequestException("transient failure")

        monkeypatch.setattr(art.requests, "get", flaky_get)
        old_ttl = art._NEGATIVE_CACHE_TTL_SECONDS
        art._NEGATIVE_CACHE_TTL_SECONDS = 0.05  # tiny TTL so the test doesn't need to sleep long
        try:
            assert art.fetch_cover_art("release-x") is None
            assert call_count["n"] == 1

            # Immediately retrying within the TTL should NOT hit the network again.
            assert art.fetch_cover_art("release-x") is None
            assert call_count["n"] == 1

            import time
            time.sleep(0.1)
            assert art.fetch_cover_art("release-x") is None
            assert call_count["n"] == 2  # TTL expired - retried for real
        finally:
            art._NEGATIVE_CACHE_TTL_SECONDS = old_ttl

    def test_success_clears_any_prior_negative_cache_entry(self, monkeypatch):
        from metamatch import art
        import requests

        state = {"fail": True}

        def sometimes_fails(url, **kwargs):
            if state["fail"]:
                raise requests.RequestException("fail once")
            class FakeResp:
                status_code = 200
                content = b"IMG"
                headers = {"Content-Type": "image/jpeg"}
            return FakeResp()

        monkeypatch.setattr(art.requests, "get", sometimes_fails)
        assert art.fetch_cover_art("release-y") is None
        assert "release-y:250" in art._negative_cache

        state["fail"] = False
        # time.monotonic() isn't epoch time - it's relative to some
        # arbitrary reference point - so simulating "long ago" means
        # subtracting from *now*, not setting an absolute low number.
        import time
        art._negative_cache["release-y:250"] = time.monotonic() - (art._NEGATIVE_CACHE_TTL_SECONDS + 1)
        result = art.fetch_cover_art("release-y")
        assert result is not None
        assert "release-y:250" not in art._negative_cache


@requires_ffmpeg
class TestToctouFingerprintCheck:
    """apply() must refuse to mutate a file whose size/mtime no longer
    match what was recorded at scan time - otherwise a match found for
    the old content could get silently applied to unrelated content that
    replaced it at the same path in the meantime."""

    def test_music_apply_refuses_tampered_file(self, music_dir, mock_music_match):
        import time
        from metamatch import MusicLibrary

        lib = MusicLibrary()
        lib.scan(str(music_dir))
        lib.match()
        target = lib.order[0]

        time.sleep(0.02)
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=880:duration=3", "-y", target],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)

        result = lib.apply(target, do_tag=True, do_rename=False)
        assert result["error"] is not None
        assert result["tagged"] is False

    def test_music_apply_succeeds_when_untampered(self, music_dir, mock_music_match):
        from metamatch import MusicLibrary

        lib = MusicLibrary()
        lib.scan(str(music_dir))
        lib.match()
        result = lib.apply(lib.order[0], do_tag=True, do_rename=False)
        assert result["error"] is None

    def test_movie_apply_refuses_tampered_file(self, movie_dir, mock_movie_match, isolated_config):
        import time
        isolated_config.set_tmdb_api_key("test-key")
        from metamatch import MovieLibrary

        lib = MovieLibrary()
        lib.scan(str(movie_dir))
        lib.match()
        target = [v for v in lib.videos_payload() if v["filename"] == "sample_movie.mp4"][0]["id"]

        time.sleep(0.02)
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=160x120:d=3",
                         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", target],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)

        result = lib.apply(target, do_tag=False, do_rename=True, do_nfo=False, do_poster=False)
        assert result["error"] is not None
        assert result["renamed"] is False

    def test_fingerprint_ignored_when_not_recorded(self):
        """Objects built directly (e.g. in other tests, or by future
        integrations) without a fingerprint shouldn't be blocked - the
        check is a best-effort safety net, not a hard requirement."""
        from metamatch.library import _fingerprint_changed
        assert _fingerprint_changed("/any/path", None, None) is False


@requires_ffmpeg
class TestPersistentUndoAcrossRestarts:
    """The whole point of the journal: undo history and successful undo
    both survive a full process restart, not just an in-memory session."""

    def test_undo_history_visible_after_simulated_restart(self, music_dir, mock_music_match, tmp_path):
        from metamatch import MusicLibrary
        from metamatch.journal import Journal

        journal_path = str(tmp_path / "restart_test.sqlite")

        # "Session 1"
        lib1 = MusicLibrary(journal=Journal(journal_path))
        lib1.scan(str(music_dir))
        lib1.match()
        target = [t for t in lib1.tracks_payload() if t["tag_artist"] == "Radiohead"][0]
        apply_result = lib1.apply(target["id"], do_tag=True, do_rename=True)
        applied_path = apply_result["new_path"]
        del lib1  # simulate the process exiting

        # "Session 2" - brand new Library, brand new Journal object, same file
        lib2 = MusicLibrary(journal=Journal(journal_path))
        lib2.scan(str(music_dir))
        payload = [t for t in lib2.tracks_payload() if t["id"] == applied_path]
        assert len(payload) == 1
        assert payload[0]["can_undo"] is True

        undo_result = lib2.undo(applied_path)
        assert undo_result["error"] is None
        assert os.path.exists(target["id"])  # original filename restored

    def test_movie_undo_history_visible_after_restart(self, movie_dir, mock_movie_match, mock_poster_download,
                                                        isolated_config, tmp_path):
        isolated_config.set_tmdb_api_key("test-key")
        from metamatch import MovieLibrary
        from metamatch.journal import Journal

        journal_path = str(tmp_path / "movie_restart.sqlite")
        lib1 = MovieLibrary(journal=Journal(journal_path))
        lib1.scan(str(movie_dir))
        lib1.match()
        target = [v for v in lib1.videos_payload() if v["filename"] == "sample_movie.mp4"][0]
        apply_result = lib1.apply(target["id"], do_tag=False, do_rename=True, do_nfo=True, do_poster=True)
        del lib1

        lib2 = MovieLibrary(journal=Journal(journal_path))
        lib2.scan(str(movie_dir))
        payload = [v for v in lib2.videos_payload() if v["id"] == apply_result["new_path"]]
        assert payload[0]["can_undo"] is True

        undo_result = lib2.undo(apply_result["new_path"])
        assert undo_result["error"] is None


@requires_ffmpeg
class TestCrashRecoveryDetection:
    """A transaction that never got a commit()/fail() call - the process
    died mid-apply - must be detected and reported on the next startup,
    not silently forgotten."""

    def test_pending_transaction_surfaced_as_recovery_notice(self, tmp_path):
        from metamatch import MusicLibrary
        from metamatch.journal import Journal

        journal_path = str(tmp_path / "crash.sqlite")
        # Simulate a crash: begin() a transaction directly (as apply() would,
        # before touching any file), then never commit/fail it.
        crashed_journal = Journal(journal_path)
        txn_id = crashed_journal.begin("music", "/tmp/x.mp3", "/tmp/x.mp3", {"artist": "Orig"}, {"do_tag": True})
        del crashed_journal

        lib = MusicLibrary(journal=Journal(journal_path))
        notices = lib.get_recovery_notices()
        assert len(notices) == 1
        assert notices[0]["id"] == txn_id
        assert notices[0]["status"] == "interrupted"
        assert notices[0]["original_path"] == "/tmp/x.mp3"

    def test_recovery_notice_only_surfaced_once(self, tmp_path):
        from metamatch import MusicLibrary
        from metamatch.journal import Journal

        journal_path = str(tmp_path / "crash2.sqlite")
        Journal(journal_path).begin("music", "/tmp/x.mp3", "/tmp/x.mp3", {}, {})

        first_startup = MusicLibrary(journal=Journal(journal_path))
        assert len(first_startup.get_recovery_notices()) == 1

        second_startup = MusicLibrary(journal=Journal(journal_path))
        assert second_startup.get_recovery_notices() == []

    def test_committed_transactions_never_show_as_recovery_notices(self, music_dir, mock_music_match, tmp_path):
        from metamatch import MusicLibrary
        from metamatch.journal import Journal

        journal_path = str(tmp_path / "clean.sqlite")
        lib1 = MusicLibrary(journal=Journal(journal_path))
        lib1.scan(str(music_dir))
        lib1.match()
        lib1.apply(lib1.order[0], do_tag=True, do_rename=False)  # completes normally
        del lib1

        lib2 = MusicLibrary(journal=Journal(journal_path))
        assert lib2.get_recovery_notices() == []

    def test_recovery_endpoint_via_flask(self, app_client, tmp_path):
        from metamatch.journal import Journal
        import app as app_module

        Journal(app_module.music_library.journal.path).begin(
            "music", "/tmp/y.mp3", "/tmp/y.mp3", {}, {"do_tag": True},
        )
        # app_client already constructed music_library before this txn was
        # written, so simulate a fresh startup's recovery check directly:
        app_module.music_library.recovered_transactions = app_module.music_library.journal.recover("music")

        resp = app_client.get("/api/recovery")
        data = resp.get_json()
        assert len(data["music"]) == 1


@requires_ffmpeg
class TestJournalSupersessionPreventsPhantomEntries:
    """A chained double-apply must supersede the earlier transaction so it
    doesn't linger as a phantom 'undoable' entry pointing at a file that
    no longer exists at that path (see journal.py Supersession tests for
    the lower-level version of this)."""

    def test_undo_all_does_not_include_superseded_stale_entries(self, music_dir, mock_music_match):
        from metamatch import MusicLibrary

        lib = MusicLibrary()
        lib.scan(str(music_dir))
        lib.match()
        target = lib.order[0]

        r1 = lib.apply(target, do_tag=True, do_rename=True)
        lib.apply(r1["new_path"], do_tag=True, do_rename=True)  # chains + supersedes r1's transaction

        undoable = lib.journal.list_undoable("music", folder=str(music_dir))
        # exactly one live entry per applied file, not one per apply() call
        current_paths = {t.current_path for t in undoable}
        assert len(undoable) == len(current_paths)


@requires_ffmpeg
@requires_ffmpeg
class TestUndoStaleFileProtection:
    """Undo must not blindly operate on a file just because a path was
    recorded for it earlier - if something replaced the file at that path
    since apply ran, undo should refuse (for the primary media file) or
    skip (for a sidecar) rather than silently rewrite/delete unrelated
    content. Same TOCTOU class apply() and quarantine() already guard
    against, extended to undo."""

    def test_music_undo_refuses_replaced_current_file(self, music_dir, mock_music_match):
        import time
        from metamatch import MusicLibrary

        lib = MusicLibrary()
        lib.scan(str(music_dir))
        lib.match()
        target = [t for t in lib.tracks_payload() if t["tag_artist"] == "Radiohead"][0]
        result = lib.apply(target["id"], do_tag=True, do_rename=False)
        assert result["error"] is None

        time.sleep(0.02)
        with open(result["new_path"], "wb") as f:
            f.write(b"UNRELATED REPLACEMENT CONTENT")

        undo_result = lib.undo(result["new_path"])
        assert undo_result["error"] is not None
        with open(result["new_path"], "rb") as f:
            assert f.read() == b"UNRELATED REPLACEMENT CONTENT"

    def test_music_undo_succeeds_when_untampered(self, music_dir, mock_music_match):
        from metamatch import MusicLibrary

        lib = MusicLibrary()
        lib.scan(str(music_dir))
        lib.match()
        result = lib.apply(lib.order[0], do_tag=True, do_rename=False)
        undo_result = lib.undo(result["new_path"])
        assert undo_result["error"] is None

    def test_undo_all_isolates_a_tampered_file_from_the_rest(self, music_dir, mock_music_match):
        import time
        from metamatch import MusicLibrary

        lib = MusicLibrary()
        lib.scan(str(music_dir))
        lib.match()
        apply_result = lib.apply_all(do_tag=True, do_rename=False, min_confidence=0)
        assert apply_result["succeeded"] == 2

        applied = lib.journal.list_undoable("music", folder=str(music_dir))
        tamper_path = applied[0].current_path
        time.sleep(0.02)
        with open(tamper_path, "wb") as f:
            f.write(b"TAMPERED")

        undo_all_result = lib.undo_all()
        assert undo_all_result["restored"] == 1
        errors = [r["error"] for r in undo_all_result["results"] if r["error"]]
        assert len(errors) == 1

    def test_movie_undo_refuses_replaced_current_video(self, movie_dir, mock_movie_match, isolated_config):
        import time
        isolated_config.set_tmdb_api_key("test-key")
        from metamatch import MovieLibrary

        lib = MovieLibrary()
        lib.scan(str(movie_dir))
        lib.match()
        target = [v for v in lib.videos_payload() if v["filename"] == "sample_movie.mp4"][0]
        result = lib.apply(target["id"], do_tag=False, do_rename=False, do_nfo=False, do_poster=False)
        assert result["error"] is None

        time.sleep(0.02)
        with open(result["new_path"], "wb") as f:
            f.write(b"UNRELATED REPLACEMENT VIDEO")

        undo_result = lib.undo(result["new_path"])
        assert undo_result["error"] is not None
        with open(result["new_path"], "rb") as f:
            assert f.read() == b"UNRELATED REPLACEMENT VIDEO"

    def test_movie_undo_does_not_delete_replaced_nfo(self, movie_dir, mock_movie_match, isolated_config):
        import time
        isolated_config.set_tmdb_api_key("test-key")
        from metamatch import MovieLibrary

        lib = MovieLibrary()
        lib.scan(str(movie_dir))
        lib.match()
        target = [v for v in lib.videos_payload() if v["filename"] == "sample_movie.mp4"][0]
        result = lib.apply(target["id"], do_tag=False, do_rename=False, do_nfo=True, do_poster=False)
        assert result["error"] is None

        time.sleep(0.02)
        with open(result["nfo_path"], "w") as f:
            f.write("REPLACED BY SOMETHING ELSE AFTER APPLY")

        undo_result = lib.undo(result["new_path"])
        assert undo_result["error"] is None  # video itself untouched, so undo still succeeds overall
        assert any("nfo" in w.lower() for w in undo_result["warnings"])
        with open(result["nfo_path"]) as f:
            assert f.read() == "REPLACED BY SOMETHING ELSE AFTER APPLY"

    def test_movie_undo_does_not_delete_replaced_poster(self, movie_dir, mock_movie_match, mock_poster_download, isolated_config):
        import time
        isolated_config.set_tmdb_api_key("test-key")
        from metamatch import MovieLibrary

        lib = MovieLibrary()
        lib.scan(str(movie_dir))
        lib.match()
        target = [v for v in lib.videos_payload() if v["filename"] == "sample_movie.mp4"][0]
        result = lib.apply(target["id"], do_tag=False, do_rename=False, do_nfo=False, do_poster=True)
        assert result["error"] is None

        time.sleep(0.02)
        with open(result["poster_path"], "wb") as f:
            f.write(b"REPLACED POSTER BYTES")

        undo_result = lib.undo(result["new_path"])
        assert undo_result["error"] is None
        assert any("poster" in w.lower() for w in undo_result["warnings"])
        with open(result["poster_path"], "rb") as f:
            assert f.read() == b"REPLACED POSTER BYTES"


@requires_ffmpeg
class TestLegacyMovieTransactionFailsClosedOnSidecars:
    """A journal transaction written before exact sidecar-path tracking
    existed has no reliable way to know where its sidecar really went (a
    rename-time collision could have pushed it to an alternate suffixed
    name) - guessing from the current filename risks the exact
    unrelated-file-deletion bug this feature exists to prevent. Undo must
    fail closed for these: skip sidecar handling entirely rather than guess."""

    def test_legacy_transaction_does_not_touch_unrelated_colliding_nfo(self, movie_dir, mock_movie_match, isolated_config, monkeypatch):
        import sqlite3
        import json
        isolated_config.set_tmdb_api_key("test-key")

        # Plant an unrelated NFO at the exact path a naive (pre-fix) undo
        # would reconstruct from the collision-safe renamed video name.
        (movie_dir / "Film (2020).mp4").write_bytes(b"unrelated other movie")
        (movie_dir / "Film (2020) (2).nfo").write_text("UNRELATED - NOT METAMATCH'S FILE")

        import metamatch.movie_matcher as movie_matcher_module
        from conftest import make_fake_movie_match
        monkeypatch.setattr(movie_matcher_module, "find_best_match", lambda video: make_fake_movie_match(title="Film", tmdb_id=1))

        from metamatch import MovieLibrary
        lib = MovieLibrary()
        lib.scan(str(movie_dir))
        lib.match()
        target = [v for v in lib.videos_payload() if v["filename"] == "sample_movie.mp4"][0]
        result = lib.apply(target["id"], do_tag=False, do_rename=True, do_nfo=False, do_poster=False)
        assert result["error"] is None

        # Simulate a transaction written before exact-path tracking existed
        # by stripping those keys out of its stored after_state.
        txn = lib.journal.get_active_for_path("movie", result["new_path"])
        conn = sqlite3.connect(lib.journal.path)
        legacy_state = {k: v for k, v in txn.after_state.items() if k not in ("nfo_path", "poster_path")}
        conn.execute("UPDATE transactions SET after_state=? WHERE id=?", (json.dumps(legacy_state), txn.id))
        conn.commit()
        conn.close()

        undo_result = lib.undo(result["new_path"])

        assert undo_result["error"] is None
        assert len(undo_result["warnings"]) == 1
        assert "predates exact sidecar tracking" in undo_result["warnings"][0]
        assert (movie_dir / "Film (2020) (2).nfo").read_text() == "UNRELATED - NOT METAMATCH'S FILE"

    def test_legacy_transaction_still_restores_video_filename(self, movie_dir, mock_movie_match, isolated_config):
        import sqlite3
        import json
        isolated_config.set_tmdb_api_key("test-key")
        from metamatch import MovieLibrary

        lib = MovieLibrary()
        lib.scan(str(movie_dir))
        lib.match()
        target = [v for v in lib.videos_payload() if v["filename"] == "sample_movie.mp4"][0]
        original_path = target["id"]
        result = lib.apply(target["id"], do_tag=False, do_rename=True, do_nfo=False, do_poster=False)

        txn = lib.journal.get_active_for_path("movie", result["new_path"])
        conn = sqlite3.connect(lib.journal.path)
        legacy_state = {k: v for k, v in txn.after_state.items() if k not in ("nfo_path", "poster_path")}
        conn.execute("UPDATE transactions SET after_state=? WHERE id=?", (json.dumps(legacy_state), txn.id))
        conn.commit()
        conn.close()

        undo_result = lib.undo(result["new_path"])
        assert undo_result["error"] is None
        assert os.path.exists(original_path)


class TestJournalFolderContainmentNotPrefixMatch:
    """undo_all() scopes to the current library's folder via
    journal.list_undoable(folder=...) - a naive string-prefix check there
    would let a sibling directory that merely shares a name prefix (e.g.
    /music vs /music_backup) leak into scope, so an "Undo all applied" in
    one library could revert files that were never part of it."""

    def test_undo_all_does_not_reach_into_prefix_sibling_folder(self, tmp_path, mock_music_match):
        import shutil
        from metamatch import MusicLibrary

        music_dir = tmp_path / "music"
        backup_dir = tmp_path / "music_backup"  # shares a string prefix with "music"
        music_dir.mkdir()
        backup_dir.mkdir()

        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                         str(music_dir / "song.mp3")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                         str(backup_dir / "other_song.mp3")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)

        # Apply a match in the backup folder using its own Library instance
        # sharing the same journal (as app.py's two libraries do).
        backup_lib = MusicLibrary()
        backup_lib.scan(str(backup_dir))
        backup_lib.match()
        backup_result = backup_lib.apply(backup_lib.order[0], do_tag=True, do_rename=False)
        assert backup_result["error"] is None

        # Now scan and apply in the real target folder, using a *different*
        # Library instance that shares the same journal file.
        from metamatch.journal import Journal
        music_lib = MusicLibrary(journal=Journal(backup_lib.journal.path))
        music_lib.scan(str(music_dir))
        music_lib.match()
        music_result = music_lib.apply(music_lib.order[0], do_tag=True, do_rename=False)
        assert music_result["error"] is None

        # Undo all, scoped to just the music folder - must not touch the
        # backup folder's file even though the names collide as strings.
        result = music_lib.undo_all()
        assert result["restored"] == 1

        from metamatch.scanner import read_track
        backup_track = read_track(str(backup_dir / "other_song.mp3"))
        assert backup_track.tag_artist == "Radiohead", "sibling folder's applied tag must survive undo_all() scoped elsewhere"


class TestHiddenAttributeNotOverriddenByDisplayCss:
    """A `.some-class { display: flex/grid }` rule silently beats the
    browser's default `[hidden] { display: none }` for any element that
    has both `hidden` and that class - author styles win over user-agent
    styles in the cascade regardless of selector specificity. This bit
    the recovery banner (and, it turned out, the progress/apply-row
    elements too): they rendered visible-but-empty on every page load
    instead of actually staying hidden. Parses the real template/CSS on
    disk so a future class added to a `hidden` element without a
    `[hidden]` override gets caught here instead of shipping."""

    def test_every_class_on_a_hidden_element_has_a_hidden_override_if_it_sets_display(self):
        import re
        from pathlib import Path

        base = Path(__file__).parent.parent
        html = (base / "templates" / "index.html").read_text()
        css = (base / "static" / "style.css").read_text()

        hidden_elements = re.findall(r'class="([^"]*)"[^>]*\bhidden\b', html)
        hidden_elements += re.findall(r'\bhidden\b[^>]*class="([^"]*)"', html)
        all_classes = set()
        for classes in hidden_elements:
            all_classes.update(classes.split())

        assert all_classes, "sanity check: the template should have at least one hidden element with a class"

        offenders = []
        for cls in sorted(all_classes):
            display_rule = re.search(rf'\.{re.escape(cls)}\s*{{[^}}]*display:\s*(\w+)', css)
            if not display_rule:
                continue  # no display: override for this class - nothing to conflict with [hidden]
            has_hidden_override = re.search(rf'\.{re.escape(cls)}\[hidden\]', css)
            if not has_hidden_override:
                offenders.append((cls, display_rule.group(1)))

        assert offenders == [], (
            f"These classes set display:<value> and are used on a `hidden` element, but have no "
            f"`.class[hidden] {{ display: none }}` override, so `hidden` will be silently ignored: {offenders}"
        )


@requires_ffmpeg
class TestMovieUndoDoesNotDeleteCollidingUnrelatedSidecar:
    """The composition bug: safe video rename + safe sidecar collision
    handling + undo reconstructing sidecar paths from scratch instead of
    using what was actually recorded = deleting a file MetaMatch never
    touched. Fixed by recording the exact sidecar paths an apply produced
    (journal after_state) instead of guessing them at undo time from the
    current video filename."""

    def _setup_collision(self, movie_dir, mock_movie_match, isolated_config, monkeypatch):
        isolated_config.set_tmdb_api_key("test-key")
        # The shared mock_movie_match fixture returns title "Test Movie" -
        # override it here so the match actually resolves to "Film (2020)",
        # matching the collision filenames planted below (otherwise the
        # rename target wouldn't collide with anything and this test would
        # silently stop testing what it claims to).
        import metamatch.movie_matcher as movie_matcher_module
        from conftest import make_fake_movie_match
        monkeypatch.setattr(
            movie_matcher_module, "find_best_match",
            lambda video: make_fake_movie_match(title="Film", tmdb_id=1),
        )

        # Force BOTH the video AND the sidecar's naive target name to
        # already be taken by something unrelated, so the sidecar has to
        # land at a doubly-suffixed name distinct from what undo would
        # naively reconstruct from the final video name.
        (movie_dir / "Film (2020).mp4").write_bytes(b"unrelated other movie")
        (movie_dir / "Film (2020) (2).nfo").write_text("UNRELATED NFO - DO NOT TOUCH")

        from metamatch import MovieLibrary
        lib = MovieLibrary()
        lib.scan(str(movie_dir))
        lib.match()
        target = [v for v in lib.videos_payload() if v["filename"] == "sample_movie.mp4"][0]
        return lib, target

    def test_movie_undo_does_not_delete_colliding_unrelated_nfo(self, movie_dir, mock_movie_match, isolated_config, monkeypatch):
        lib, target = self._setup_collision(movie_dir, mock_movie_match, isolated_config, monkeypatch)
        unrelated_nfo = movie_dir / "Film (2020) (2).nfo"

        apply_result = lib.apply(target["id"], do_tag=False, do_rename=True, do_nfo=True, do_poster=False)
        assert unrelated_nfo.exists()  # sanity: still there right after apply

        lib.undo(apply_result["new_path"])

        assert unrelated_nfo.exists(), "undo deleted a file it never created"
        assert unrelated_nfo.read_text() == "UNRELATED NFO - DO NOT TOUCH"

    def test_movie_undo_removes_actual_collision_safe_nfo(self, movie_dir, mock_movie_match, isolated_config, monkeypatch):
        lib, target = self._setup_collision(movie_dir, mock_movie_match, isolated_config, monkeypatch)

        apply_result = lib.apply(target["id"], do_tag=False, do_rename=True, do_nfo=True, do_poster=False)
        real_nfo_path = apply_result["nfo_path"]
        assert real_nfo_path.endswith("Film (2020) (2) (2).nfo")
        assert os.path.exists(real_nfo_path)

        lib.undo(apply_result["new_path"])

        assert not os.path.exists(real_nfo_path), "the sidecar MetaMatch actually created should be cleaned up"

    def test_movie_undo_does_not_delete_colliding_unrelated_poster(self, movie_dir, mock_movie_match,
                                                                     mock_poster_download, isolated_config, monkeypatch):
        isolated_config.set_tmdb_api_key("test-key")
        import metamatch.movie_matcher as movie_matcher_module
        from conftest import make_fake_movie_match
        monkeypatch.setattr(
            movie_matcher_module, "find_best_match",
            lambda video: make_fake_movie_match(title="Film", tmdb_id=1),
        )
        (movie_dir / "Film (2020).mp4").write_bytes(b"unrelated other movie")
        (movie_dir / "Film (2020) (2)-poster.jpg").write_bytes(b"UNRELATED POSTER BYTES")

        from metamatch import MovieLibrary
        lib = MovieLibrary()
        lib.scan(str(movie_dir))
        lib.match()
        target = [v for v in lib.videos_payload() if v["filename"] == "sample_movie.mp4"][0]

        unrelated_poster = movie_dir / "Film (2020) (2)-poster.jpg"
        apply_result = lib.apply(target["id"], do_tag=False, do_rename=True, do_nfo=False, do_poster=True)
        assert unrelated_poster.exists()

        lib.undo(apply_result["new_path"])

        assert unrelated_poster.exists(), "undo deleted an unrelated poster it never created"
        assert unrelated_poster.read_bytes() == b"UNRELATED POSTER BYTES"

    def test_after_state_recorded_on_commit(self, movie_dir, mock_movie_match, isolated_config):
        """The underlying mechanism: a successful apply's journal transaction
        must carry the real sidecar paths, not leave callers to guess them."""
        isolated_config.set_tmdb_api_key("test-key")
        from metamatch import MovieLibrary

        lib = MovieLibrary()
        lib.scan(str(movie_dir))
        lib.match()
        target = lib.order[0]
        apply_result = lib.apply(target, do_tag=False, do_rename=False, do_nfo=True, do_poster=False)

        txn = lib.journal.get_active_for_path("movie", apply_result["new_path"])
        assert txn.after_state["nfo_path"] == apply_result["nfo_path"]


class TestQuarantineFingerprintCheck:
    """quarantine() checks the caller supplied a path from the current
    scan, but (until this fix) never verified the file at that path still
    matches what was scanned - a file replaced at the same path between
    scan and quarantine would get moved without anyone checking it's still
    the same content that was actually flagged as a duplicate."""

    def test_music_quarantine_refuses_tampered_file(self, music_dir):
        import time
        from metamatch import MusicLibrary

        lib = MusicLibrary()
        lib.scan(str(music_dir))
        target = lib.order[0]

        time.sleep(0.02)
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=880:duration=3", "-y", target],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)

        result = lib.quarantine([target])
        assert result["moved"] == 0
        assert result["results"][0]["error"] is not None
        assert os.path.exists(target)  # never moved

    def test_music_quarantine_succeeds_when_untampered(self, music_dir):
        from metamatch import MusicLibrary

        lib = MusicLibrary()
        lib.scan(str(music_dir))
        result = lib.quarantine([lib.order[0]])
        assert result["moved"] == 1

    def test_movie_quarantine_refuses_tampered_file(self, movie_dir):
        import time
        from metamatch import MovieLibrary

        lib = MovieLibrary()
        lib.scan(str(movie_dir))
        target = [p for p in lib.order if p.endswith("sample_movie.mp4")][0]

        time.sleep(0.02)
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=160x120:d=3",
                         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", target],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)

        result = lib.quarantine([target])
        assert result["moved"] == 0
        assert os.path.exists(target)


class TestCrossOriginRejection:
    def test_forged_cross_origin_post_is_blocked(self, app_client):
        resp = app_client.post("/api/scan", json={"folder": "/nonexistent"},
                                headers={"Origin": "http://evil.example.com"})
        assert resp.status_code == 403

    def test_no_origin_header_is_allowed_through(self, app_client, tmp_path):
        resp = app_client.post("/api/scan", json={"folder": str(tmp_path)})
        assert resp.status_code != 403

    def test_get_requests_are_never_blocked(self, app_client):
        resp = app_client.get("/api/tracks", headers={"Origin": "http://evil.example.com"})
        assert resp.status_code != 403
