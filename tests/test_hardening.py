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

        record = lib.undo_by_path[r2["new_path"]]
        assert record["original_path"] == original_path

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

        assert lib.undo_by_path[r3["new_path"]]["original_path"] == original_path


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
