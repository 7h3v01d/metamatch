import os

import pytest

from conftest import requires_ffmpeg


@requires_ffmpeg
class TestScanRoute:
    def test_scan_finds_files(self, app_client, music_dir):
        resp = app_client.post("/api/scan", json={"folder": str(music_dir), "recursive": True})
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["count"] == 2

    def test_scan_missing_folder_returns_400(self, app_client, tmp_path):
        resp = app_client.post("/api/scan", json={"folder": str(tmp_path / "nope")})
        assert resp.status_code == 400

    def test_scan_empty_folder_path_returns_400(self, app_client):
        resp = app_client.post("/api/scan", json={"folder": ""})
        assert resp.status_code == 400

    def test_scan_resets_undo_state(self, app_client, music_dir):
        app_client.post("/api/scan", json={"folder": str(music_dir)})
        import app as app_module
        app_module.STATE["undo_by_path"]["stale"] = {"fake": "record"}
        app_client.post("/api/scan", json={"folder": str(music_dir)})
        assert app_module.STATE["undo_by_path"] == {}


@requires_ffmpeg
class TestMatchFlow:
    def test_match_start_requires_prior_scan(self, app_client):
        resp = app_client.post("/api/match/start")
        assert resp.status_code == 400

    def test_full_match_flow(self, app_client, music_dir, mock_music_match, wait_for_progress):
        app_client.post("/api/scan", json={"folder": str(music_dir)})
        start_resp = app_client.post("/api/match/start")
        assert start_resp.status_code == 200

        wait_for_progress(app_client, "/api/match/progress")
        tracks = app_client.get("/api/tracks").get_json()["tracks"]
        assert all(t["match"]["confidence"] == 92.5 for t in tracks)

    def test_cannot_start_match_twice_concurrently(self, app_client, music_dir, mock_music_match):
        app_client.post("/api/scan", json={"folder": str(music_dir)})
        import app as app_module
        app_module.STATE["match_progress"]["running"] = True
        resp = app_client.post("/api/match/start")
        assert resp.status_code == 409


@requires_ffmpeg
class TestApplyAndUndo:
    def _scanned_and_matched(self, app_client, music_dir, mock_music_match, wait_for_progress):
        app_client.post("/api/scan", json={"folder": str(music_dir)})
        app_client.post("/api/match/start")
        wait_for_progress(app_client, "/api/match/progress")
        return app_client.get("/api/tracks").get_json()["tracks"]

    def test_apply_unknown_track_404s(self, app_client):
        resp = app_client.post("/api/apply", json={"id": "/nonexistent/path.mp3"})
        assert resp.status_code == 404

    def test_apply_without_match_400s(self, app_client, music_dir):
        app_client.post("/api/scan", json={"folder": str(music_dir)})
        tracks = app_client.get("/api/tracks").get_json()["tracks"]
        resp = app_client.post("/api/apply", json={"id": tracks[0]["id"]})
        assert resp.status_code == 400

    def test_apply_tags_and_rename(self, app_client, music_dir, mock_music_match, wait_for_progress):
        tracks = self._scanned_and_matched(app_client, music_dir, mock_music_match, wait_for_progress)
        target = tracks[0]

        resp = app_client.post("/api/apply", json={"id": target["id"], "tag": True, "rename": True})
        result = resp.get_json()
        assert result["error"] is None
        assert result["tagged"] and result["renamed"]
        assert os.path.exists(result["new_path"])
        assert os.path.basename(result["new_path"]) == "Radiohead - Karma Police.mp3"

    def test_apply_with_art(self, app_client, music_dir, mock_music_match, mock_cover_art, wait_for_progress):
        tracks = self._scanned_and_matched(app_client, music_dir, mock_music_match, wait_for_progress)
        target = tracks[0]

        resp = app_client.post("/api/apply", json={"id": target["id"], "tag": True, "rename": False, "art": True})
        result = resp.get_json()
        assert result["art_embedded"] is True

        from mutagen.id3 import ID3
        tags = ID3(result["new_path"])
        assert len(tags.getall("APIC")) == 1

    def test_apply_then_undo_restores_original(self, app_client, music_dir, mock_music_match, wait_for_progress):
        tracks = self._scanned_and_matched(app_client, music_dir, mock_music_match, wait_for_progress)
        target = tracks[0]
        original_path = target["id"]

        apply_result = app_client.post("/api/apply", json={"id": target["id"], "tag": True, "rename": True}).get_json()
        new_path = apply_result["new_path"]

        undo_resp = app_client.post("/api/undo", json={"id": new_path})
        undo_result = undo_resp.get_json()
        assert undo_result["error"] is None
        assert os.path.exists(original_path)
        assert not os.path.exists(new_path)

    def test_undo_with_no_record_400s(self, app_client, music_dir):
        app_client.post("/api/scan", json={"folder": str(music_dir)})
        tracks = app_client.get("/api/tracks").get_json()["tracks"]
        resp = app_client.post("/api/undo", json={"id": tracks[0]["id"]})
        assert resp.status_code == 400

    def test_apply_all_respects_threshold(self, app_client, music_dir, mock_music_match, wait_for_progress):
        self._scanned_and_matched(app_client, music_dir, mock_music_match, wait_for_progress)

        below = app_client.post("/api/apply_all", json={"tag": True, "rename": False, "min_confidence": 99}).get_json()
        assert below["applied"] == 0

        above = app_client.post("/api/apply_all", json={"tag": True, "rename": False, "min_confidence": 50}).get_json()
        assert above["applied"] == 2

    def test_undo_all_reverts_everything(self, app_client, music_dir, mock_music_match, wait_for_progress):
        self._scanned_and_matched(app_client, music_dir, mock_music_match, wait_for_progress)
        app_client.post("/api/apply_all", json={"tag": True, "rename": True, "min_confidence": 0})

        undo_resp = app_client.post("/api/undo_all")
        result = undo_resp.get_json()
        assert result["restored"] == 2

        tracks = app_client.get("/api/tracks").get_json()["tracks"]
        assert all(not t["can_undo"] for t in tracks)


@requires_ffmpeg
class TestDuplicates:
    def test_scan_requires_prior_folder_scan(self, app_client):
        resp = app_client.post("/api/duplicates/scan")
        assert resp.status_code == 400

    def test_finds_exact_duplicate(self, app_client, tmp_path, music_dir):
        import shutil
        shutil.copy(music_dir / "tagged.mp3", music_dir / "tagged_copy.mp3")
        app_client.post("/api/scan", json={"folder": str(music_dir)})

        resp = app_client.post("/api/duplicates/scan")
        data = resp.get_json()
        assert len(data["exact"]) == 1
        assert len(data["exact"][0]["files"]) == 2

    def test_quarantine_moves_file(self, app_client, music_dir):
        import shutil
        shutil.copy(music_dir / "tagged.mp3", music_dir / "tagged_copy.mp3")
        app_client.post("/api/scan", json={"folder": str(music_dir)})
        dup_data = app_client.post("/api/duplicates/scan").get_json()
        paths = [f["path"] for f in dup_data["exact"][0]["files"]]

        resp = app_client.post("/api/duplicates/quarantine", json={"paths": [paths[1]]})
        result = resp.get_json()
        assert result["moved"] == 1
        assert not os.path.exists(paths[1])

    def test_quarantine_without_paths_400s(self, app_client, music_dir):
        app_client.post("/api/scan", json={"folder": str(music_dir)})
        resp = app_client.post("/api/duplicates/quarantine", json={"paths": []})
        assert resp.status_code == 400


@requires_ffmpeg
class TestExportCsv:
    def test_export_returns_csv(self, app_client, music_dir, mock_music_match, wait_for_progress):
        app_client.post("/api/scan", json={"folder": str(music_dir)})
        app_client.post("/api/match/start")
        wait_for_progress(app_client, "/api/match/progress")

        resp = app_client.get("/api/export_csv")
        assert resp.status_code == 200
        assert resp.mimetype == "text/csv"
        body = resp.data.decode()
        assert "Radiohead" in body
        assert "confidence" in body.lower()


class TestArtRoute:
    def test_missing_art_returns_404(self, app_client, mock_cover_art, monkeypatch):
        import app as app_module
        monkeypatch.setattr(app_module, "fetch_cover_art", lambda *a, **k: None)
        resp = app_client.get("/api/art/some-release-id")
        assert resp.status_code == 404

    def test_found_art_returns_image_bytes(self, app_client, mock_cover_art):
        resp = app_client.get("/api/art/some-release-id")
        assert resp.status_code == 200
        assert resp.data == mock_cover_art


def test_index_page_loads(app_client):
    resp = app_client.get("/")
    assert resp.status_code == 200
    assert b"MetaMatch" in resp.data
