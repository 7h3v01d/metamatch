import os

import pytest

from conftest import requires_ffmpeg


class TestTmdbSettings:
    def test_unconfigured_by_default(self, app_client):
        resp = app_client.get("/api/settings/tmdb")
        data = resp.get_json()
        assert data["configured"] is False
        assert data["masked_key"] is None

    def test_save_key_then_reports_configured(self, app_client):
        resp = app_client.post("/api/settings/tmdb", json={"api_key": "test-key-12345"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["configured"] is True
        assert "test" in data["masked_key"]

        follow_up = app_client.get("/api/settings/tmdb").get_json()
        assert follow_up["configured"] is True

    def test_empty_key_rejected(self, app_client):
        resp = app_client.post("/api/settings/tmdb", json={"api_key": ""})
        assert resp.status_code == 400

    def test_reports_ffmpeg_ffprobe_availability(self, app_client):
        data = app_client.get("/api/settings/tmdb").get_json()
        assert "ffmpeg_available" in data
        assert "ffprobe_available" in data


@requires_ffmpeg
class TestMovieScanRoute:
    def test_scan_finds_files(self, app_client, movie_dir):
        resp = app_client.post("/api/movies/scan", json={"folder": str(movie_dir), "recursive": True})
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["count"] == 2
        assert "ffprobe_available" in data

    def test_scan_missing_folder_400s(self, app_client, tmp_path):
        resp = app_client.post("/api/movies/scan", json={"folder": str(tmp_path / "nope")})
        assert resp.status_code == 400


@requires_ffmpeg
class TestMovieMatchGating:
    def test_match_blocked_without_api_key(self, app_client, movie_dir):
        app_client.post("/api/movies/scan", json={"folder": str(movie_dir)})
        resp = app_client.post("/api/movies/match/start")
        assert resp.status_code == 400
        assert "TMDB" in resp.get_json()["error"]

    def test_match_blocked_without_prior_scan(self, app_client):
        app_client.post("/api/settings/tmdb", json={"api_key": "test-key"})
        resp = app_client.post("/api/movies/match/start")
        assert resp.status_code == 400


@requires_ffmpeg
class TestMovieMatchFlow:
    def test_full_match_flow(self, app_client, movie_dir, mock_movie_match, wait_for_progress):
        app_client.post("/api/settings/tmdb", json={"api_key": "test-key"})
        app_client.post("/api/movies/scan", json={"folder": str(movie_dir)})
        start_resp = app_client.post("/api/movies/match/start")
        assert start_resp.status_code == 200

        progress = wait_for_progress(app_client, "/api/movies/match/progress")
        assert progress["error"] is None

        videos = app_client.get("/api/movies").get_json()["videos"]
        assert all(v["match"]["confidence"] == 92.0 for v in videos)


@requires_ffmpeg
class TestMovieApplyAndUndo:
    def _scanned_and_matched(self, app_client, movie_dir, mock_movie_match, wait_for_progress):
        app_client.post("/api/settings/tmdb", json={"api_key": "test-key"})
        app_client.post("/api/movies/scan", json={"folder": str(movie_dir)})
        app_client.post("/api/movies/match/start")
        wait_for_progress(app_client, "/api/movies/match/progress")
        return app_client.get("/api/movies").get_json()["videos"]

    def test_apply_unknown_file_404s(self, app_client):
        resp = app_client.post("/api/movies/apply", json={"id": "/nowhere.mp4"})
        assert resp.status_code == 404

    def test_apply_without_match_400s(self, app_client, movie_dir):
        app_client.post("/api/movies/scan", json={"folder": str(movie_dir)})
        videos = app_client.get("/api/movies").get_json()["videos"]
        resp = app_client.post("/api/movies/apply", json={"id": videos[0]["id"]})
        assert resp.status_code == 400

    def test_apply_rename_nfo_poster(self, app_client, movie_dir, mock_movie_match, mock_poster_download, wait_for_progress):
        videos = self._scanned_and_matched(app_client, movie_dir, mock_movie_match, wait_for_progress)
        target = [v for v in videos if v["filename"] == "sample_movie.mp4"][0]

        resp = app_client.post("/api/movies/apply", json={
            "id": target["id"], "tag": False, "rename": True, "nfo": True, "poster": True,
        })
        result = resp.get_json()
        assert result["error"] is None
        assert result["renamed"] is True
        assert os.path.exists(result["new_path"])
        assert os.path.exists(result["nfo_path"])
        assert os.path.exists(result["poster_path"])

    def test_apply_embeds_mp4_tags(self, app_client, movie_dir, mock_movie_match, wait_for_progress):
        videos = self._scanned_and_matched(app_client, movie_dir, mock_movie_match, wait_for_progress)
        target = [v for v in videos if v["filename"] == "sample_movie.mp4"][0]

        resp = app_client.post("/api/movies/apply", json={
            "id": target["id"], "tag": True, "rename": False, "nfo": False, "poster": False,
        })
        result = resp.get_json()
        assert result["tagged"] is True

        from mutagen.mp4 import MP4
        audio = MP4(result["new_path"])
        assert audio["\xa9nam"][0] == "Test Movie"

    def test_apply_then_undo_restores_filename_and_removes_created_sidecars(
        self, app_client, movie_dir, mock_movie_match, mock_poster_download, wait_for_progress,
    ):
        videos = self._scanned_and_matched(app_client, movie_dir, mock_movie_match, wait_for_progress)
        target = [v for v in videos if v["filename"] == "sample_movie.mp4"][0]
        original_path = target["id"]

        apply_result = app_client.post("/api/movies/apply", json={
            "id": target["id"], "tag": True, "rename": True, "nfo": True, "poster": True,
        }).get_json()
        new_path = apply_result["new_path"]

        undo_result = app_client.post("/api/movies/undo", json={"id": new_path}).get_json()
        assert undo_result["error"] is None
        assert os.path.exists(original_path)
        assert not os.path.exists(new_path)

        base = os.path.splitext(original_path)[0]
        assert not os.path.exists(base + ".nfo")
        assert not os.path.exists(base + "-poster.jpg")

        from mutagen.mp4 import MP4
        audio = MP4(original_path)
        assert not audio.get("\xa9nam")

    def test_undo_preserves_preexisting_nfo(self, app_client, movie_dir, mock_movie_match, wait_for_progress):
        nfo_path = str(movie_dir / "sample_movie.nfo")
        with open(nfo_path, "w") as f:
            f.write("<movie><title>Pre-existing</title></movie>")

        videos = self._scanned_and_matched(app_client, movie_dir, mock_movie_match, wait_for_progress)
        target = [v for v in videos if v["filename"] == "sample_movie.mp4"][0]

        app_client.post("/api/movies/apply", json={
            "id": target["id"], "tag": False, "rename": False, "nfo": True, "poster": False,
        })
        app_client.post("/api/movies/undo", json={"id": target["id"]})

        assert os.path.exists(nfo_path)
        # Undo restores the pre-existing sidecar's actual original content,
        # not just its presence - see tests/test_hardening.py for more on
        # the fix that made this possible (byte-snapshotting sidecars
        # before overwriting them).
        with open(nfo_path) as f:
            assert f.read() == "<movie><title>Pre-existing</title></movie>"

    def test_apply_all_respects_threshold(self, app_client, movie_dir, mock_movie_match, wait_for_progress):
        self._scanned_and_matched(app_client, movie_dir, mock_movie_match, wait_for_progress)

        below = app_client.post("/api/movies/apply_all", json={
            "rename": False, "nfo": False, "poster": False, "min_confidence": 99,
        }).get_json()
        assert below["attempted"] == 0

        above = app_client.post("/api/movies/apply_all", json={
            "rename": False, "nfo": False, "poster": False, "min_confidence": 50,
        }).get_json()
        assert above["succeeded"] == 2

    def test_undo_all(self, app_client, movie_dir, mock_movie_match, mock_poster_download, wait_for_progress):
        self._scanned_and_matched(app_client, movie_dir, mock_movie_match, wait_for_progress)
        app_client.post("/api/movies/apply_all", json={
            "rename": True, "nfo": True, "poster": True, "min_confidence": 0,
        })

        result = app_client.post("/api/movies/undo_all").get_json()
        assert result["restored"] == 2

        videos = app_client.get("/api/movies").get_json()["videos"]
        assert all(not v["can_undo"] for v in videos)


@requires_ffmpeg
class TestMovieDuplicates:
    def test_scan_requires_prior_folder_scan(self, app_client):
        resp = app_client.post("/api/movies/duplicates/scan")
        assert resp.status_code == 400

    def test_finds_exact_duplicate(self, app_client, movie_dir):
        import shutil
        shutil.copy(movie_dir / "sample_movie.mp4", movie_dir / "sample_movie_copy.mp4")
        app_client.post("/api/movies/scan", json={"folder": str(movie_dir)})

        resp = app_client.post("/api/movies/duplicates/scan")
        data = resp.get_json()
        assert len(data["exact"]) == 1
        assert len(data["exact"][0]["files"]) == 2

    def test_quarantine_sweeps_sidecars(self, app_client, movie_dir):
        import shutil
        dup_path = movie_dir / "sample_movie_copy.mp4"
        shutil.copy(movie_dir / "sample_movie.mp4", dup_path)
        with open(str(dup_path).replace(".mp4", ".nfo"), "w") as f:
            f.write("<movie/>")

        app_client.post("/api/movies/scan", json={"folder": str(movie_dir)})
        dup_data = app_client.post("/api/movies/duplicates/scan").get_json()
        paths = [f["path"] for f in dup_data["exact"][0]["files"]]
        target = [p for p in paths if "copy" in p]

        resp = app_client.post("/api/movies/duplicates/quarantine", json={"paths": target})
        result = resp.get_json()
        assert result["moved"] == 2  # video + its .nfo sidecar
        assert os.path.exists(os.path.join(str(movie_dir), "_metamatch_duplicates", "sample_movie_copy.mp4"))
        assert os.path.exists(os.path.join(str(movie_dir), "_metamatch_duplicates", "sample_movie_copy.nfo"))


@requires_ffmpeg
class TestMovieExportCsv:
    def test_export_returns_csv(self, app_client, movie_dir, mock_movie_match, wait_for_progress):
        app_client.post("/api/settings/tmdb", json={"api_key": "test-key"})
        app_client.post("/api/movies/scan", json={"folder": str(movie_dir)})
        app_client.post("/api/movies/match/start")
        wait_for_progress(app_client, "/api/movies/match/progress")

        resp = app_client.get("/api/movies/export_csv")
        assert resp.status_code == 200
        assert resp.mimetype == "text/csv"
        assert "Test Movie" in resp.data.decode()
