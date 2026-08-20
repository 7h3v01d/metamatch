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

    def test_rescanning_preserves_undo_history(self, app_client, music_dir, mock_music_match, wait_for_progress):
        """Undo history is journal-backed now, not reset by every scan - that's
        the whole point of persistence: a file applied earlier still shows
        can_undo=True after a rescan (or, in the real app, after a restart)."""
        app_client.post("/api/scan", json={"folder": str(music_dir)})
        app_client.post("/api/match/start")
        wait_for_progress(app_client, "/api/match/progress")
        tracks = app_client.get("/api/tracks").get_json()["tracks"]
        target_id = tracks[0]["id"]

        apply_result = app_client.post("/api/apply", json={"id": target_id, "tag": True, "rename": False}).get_json()
        assert apply_result["error"] is None

        # rescan the same folder - the applied file's undo history must survive
        app_client.post("/api/scan", json={"folder": str(music_dir)})
        tracks_after = app_client.get("/api/tracks").get_json()["tracks"]
        rescanned = [t for t in tracks_after if t["id"] == apply_result["new_path"]][0]
        assert rescanned["can_undo"] is True


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
        app_module.music_library.match_progress["running"] = True
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
        assert below["attempted"] == 0

        above = app_client.post("/api/apply_all", json={"tag": True, "rename": False, "min_confidence": 50}).get_json()
        assert above["succeeded"] == 2

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


class TestBrowseRoute:
    def test_defaults_to_home_directory(self, app_client):
        import os
        resp = app_client.get("/api/browse")
        data = resp.get_json()
        assert data["path"] == os.path.expanduser("~")

    def test_lists_subdirectories_of_given_path(self, app_client, tmp_path):
        (tmp_path / "alpha").mkdir()
        (tmp_path / "beta").mkdir()
        (tmp_path / "not_a_dir.txt").write_text("x")

        resp = app_client.get("/api/browse", query_string={"path": str(tmp_path)})
        data = resp.get_json()
        assert sorted(data["directories"]) == ["alpha", "beta"]
        assert "not_a_dir.txt" not in data["directories"]

    def test_hides_dotfiles(self, app_client, tmp_path):
        (tmp_path / ".hidden").mkdir()
        (tmp_path / "visible").mkdir()

        resp = app_client.get("/api/browse", query_string={"path": str(tmp_path)})
        data = resp.get_json()
        assert data["directories"] == ["visible"]

    def test_parent_navigation(self, app_client, tmp_path):
        child = tmp_path / "child"
        child.mkdir()
        resp = app_client.get("/api/browse", query_string={"path": str(child)})
        data = resp.get_json()
        assert data["parent"] == str(tmp_path)

    def test_root_has_no_parent(self, app_client):
        import os
        resp = app_client.get("/api/browse", query_string={"path": "/"})
        data = resp.get_json()
        if os.name == "nt":
            # On Windows, "/" resolves to the root of the current drive
            # rather than a single POSIX-style universal root - if the
            # machine has more than one drive, "up" from there correctly
            # offers the drive-switcher (see TestBrowseWindowsDrives)
            # instead of a dead end, so parent isn't necessarily None here.
            import app as app_module
            assert data["parent"] in (None, app_module.DRIVES_SENTINEL)
        else:
            assert data["parent"] is None

    def test_invalid_path_falls_back_to_home_instead_of_erroring(self, app_client, tmp_path):
        import os
        resp = app_client.get("/api/browse", query_string={"path": str(tmp_path / "does_not_exist")})
        assert resp.status_code == 200
        assert resp.get_json()["path"] == os.path.expanduser("~")

    def test_results_sorted_case_insensitively(self, app_client, tmp_path):
        for name in ["Zebra", "apple", "Banana"]:
            (tmp_path / name).mkdir()
        resp = app_client.get("/api/browse", query_string={"path": str(tmp_path)})
        assert resp.get_json()["directories"] == ["apple", "Banana", "Zebra"]


class TestBrowseWindowsDrives:
    """Windows has no single filesystem root (C:\\'s parent is itself), so
    navigating "up" from a drive root needs a virtual drive-list view
    instead of a dead end. Simulated here via monkeypatching os.name/
    os.path.exists since this suite normally runs on Linux/macOS."""

    def _simulate_windows(self, monkeypatch, drives):
        import os
        import re
        monkeypatch.setattr(os, "name", "nt")
        real_exists = os.path.exists
        drive_pattern = re.compile(r"^[A-Za-z]:\\$")

        def fake_exists(p):
            # Fully isolate drive-letter-root checks from whatever drives
            # actually exist on the machine running the suite - falling
            # back to the real os.path.exists() for *those specific*
            # paths would let a dev box's real D:\, E:\, etc. leak into
            # "only these fake drives exist" simulations. Non-drive paths
            # still use the real check, since other code paths may need it.
            if drive_pattern.match(str(p)):
                return p in drives
            return real_exists(p)

        monkeypatch.setattr(os.path, "exists", fake_exists)

    def test_lists_multiple_drives(self, app_client, monkeypatch):
        self._simulate_windows(monkeypatch, ["C:\\", "D:\\", "E:\\"])
        import app as app_module
        drives = app_module._list_windows_drives()
        assert drives == ["C:\\", "D:\\", "E:\\"]

    def test_non_windows_returns_no_drives(self, app_client, monkeypatch):
        import os
        monkeypatch.setattr(os, "name", "posix")
        import app as app_module
        assert app_module._list_windows_drives() == []

    def test_drives_sentinel_returns_drive_list_response(self, app_client, monkeypatch):
        self._simulate_windows(monkeypatch, ["C:\\", "D:\\"])
        import app as app_module
        resp = app_client.get("/api/browse", query_string={"path": app_module.DRIVES_SENTINEL})
        data = resp.get_json()
        assert data["is_drive_list"] is True
        assert data["directories"] == ["C:\\", "D:\\"]
        assert data["parent"] is None
        assert data["path"] == "This PC"

    def test_drive_root_parent_points_at_drives_sentinel_when_multiple_drives(self, app_client, monkeypatch, tmp_path):
        # Simulate tmp_path itself acting as a "drive root": its dirname
        # equals itself is the real trigger condition, which we can't
        # fake for an arbitrary tmp_path, so instead verify the logic
        # directly against a real POSIX root with multiple "drives" faked.
        self._simulate_windows(monkeypatch, ["C:\\", "D:\\"])
        import os
        real_dirname = os.path.dirname
        monkeypatch.setattr(os.path, "dirname", lambda p: p if p == "C:\\" else real_dirname(p))
        # os.path.isdir also needs to accept "C:\\" as a real directory for this test
        real_isdir = os.path.isdir
        monkeypatch.setattr(os.path, "isdir", lambda p: True if p == "C:\\" else real_isdir(p))
        real_abspath = os.path.abspath
        monkeypatch.setattr(os.path, "abspath", lambda p: "C:\\" if p == "C:\\" else real_abspath(p))
        real_scandir = os.scandir

        class _EmptyScandir:
            def __enter__(self): return iter([])
            def __exit__(self, *a): return False

        monkeypatch.setattr(os, "scandir", lambda p: _EmptyScandir() if p == "C:\\" else real_scandir(p))

        import app as app_module
        resp = app_client.get("/api/browse", query_string={"path": "C:\\"})
        data = resp.get_json()
        assert data["parent"] == app_module.DRIVES_SENTINEL

    def test_drive_root_parent_is_none_with_only_one_drive(self, app_client, monkeypatch):
        self._simulate_windows(monkeypatch, ["C:\\"])
        import os
        real_dirname = os.path.dirname
        monkeypatch.setattr(os.path, "dirname", lambda p: p if p == "C:\\" else real_dirname(p))
        real_isdir = os.path.isdir
        monkeypatch.setattr(os.path, "isdir", lambda p: True if p == "C:\\" else real_isdir(p))
        real_abspath = os.path.abspath
        monkeypatch.setattr(os.path, "abspath", lambda p: "C:\\" if p == "C:\\" else real_abspath(p))
        real_scandir = os.scandir

        class _EmptyScandir:
            def __enter__(self): return iter([])
            def __exit__(self, *a): return False

        monkeypatch.setattr(os, "scandir", lambda p: _EmptyScandir() if p == "C:\\" else real_scandir(p))

        resp = app_client.get("/api/browse", query_string={"path": "C:\\"})
        data = resp.get_json()
        assert data["parent"] is None
