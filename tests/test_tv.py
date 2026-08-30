"""
test_tv.py
Tests for the TV-episode vertical: filename parsing, TMDB matching (mocked),
end-to-end apply/undo through TvLibrary, failure rollback, and the Flask
/api/tv/* routes. The apply/undo/rollback tests deliberately parallel the
movie tests, because TV reuses the same journal + rollback machinery and the
guarantee under test is identical: an apply either completes or leaves the
file exactly as it was.
"""

from __future__ import annotations

import os

import pytest

from conftest import requires_ffmpeg
from metamatch.journal import Journal, ROLLED_BACK, RECOVERY_REQUIRED


# ---------------------------------------------------------------------------
# Filename parsing (no ffmpeg needed)
# ---------------------------------------------------------------------------

class TestEpisodeParsing:
    def _p(self, filename, parent=""):
        from metamatch.episode_scanner import _parse_filename
        return _parse_filename(filename, parent)

    def test_standard_sxxeyy(self):
        r = self._p("Breaking.Bad.S01E02.Cat.in.the.Bag.1080p.BluRay.x264-GROUP.mkv")
        assert r["series_guess"] == "Breaking Bad"
        assert r["season"] == 1 and r["episode"] == 2
        assert r["episode_title_guess"] == "Cat in the Bag"

    def test_nxnn_form(self):
        r = self._p("The Office - 1x02 - Diversity Day.mkv")
        assert r["series_guess"] == "The Office"
        assert (r["season"], r["episode"]) == (1, 2)

    def test_multi_episode(self):
        r = self._p("Firefly.S01E02E03.The.Train.Job.mkv")
        assert r["episodes"] == [2, 3]
        assert r["episode"] == 2  # primary is the first

    def test_season_subfolder_supplies_series_and_season(self):
        r = self._p("S01E05.mkv", "/media/Better Call Saul/Season 01")
        assert r["series_guess"] == "Better Call Saul"
        assert (r["season"], r["episode"]) == (1, 5)

    def test_episode_only_with_season_folder(self):
        r = self._p("E07.mkv", "/media/Fargo/Season 3")
        assert r["series_guess"] == "Fargo"
        assert (r["season"], r["episode"]) == (3, 7)

    def test_non_episode_is_unparsed(self):
        r = self._p("Some.Movie.2020.1080p.mkv")
        assert r["season"] is None and r["episode"] is None


# ---------------------------------------------------------------------------
# Matching (mocked TMDB)
# ---------------------------------------------------------------------------

class TestTvMatching:
    def test_series_scoring_prefers_exact_name(self):
        from metamatch.tv_matcher import score_series
        from metamatch.episode_scanner import EpisodeFile

        ep = EpisodeFile(path="x", filename="x", ext=".mkv", size_bytes=1,
                         series_guess="Breaking Bad", season=1, episode=2)
        good = score_series(ep, {"id": 1, "name": "Breaking Bad", "first_air_date": "2008-01-20"}, rank=0)
        bad = score_series(ep, {"id": 2, "name": "Completely Different", "first_air_date": "2010-01-01"}, rank=1)
        assert good["series_confidence"] > bad["series_confidence"]

    def test_unparsed_file_yields_no_match(self, monkeypatch):
        from metamatch import tv_matcher
        from metamatch.episode_scanner import EpisodeFile
        ep = EpisodeFile(path="x", filename="x", ext=".mkv", size_bytes=1)  # nothing parsed
        assert tv_matcher.find_best_match(ep) is None

    def test_missing_episode_falls_back_with_penalty(self, monkeypatch):
        from metamatch import tv_matcher
        from metamatch.episode_scanner import EpisodeFile

        monkeypatch.setattr(tv_matcher, "_tmdb_search_series",
                            lambda series, limit=5: [{"id": 9, "name": "Real Show", "first_air_date": "2011-01-01"}])
        # series exists, but the episode endpoint 404s
        monkeypatch.setattr(tv_matcher, "_tmdb_episode", lambda sid, s, e: None)

        ep = EpisodeFile(path="x", filename="x", ext=".mkv", size_bytes=1,
                         series_guess="Real Show", season=9, episode=99)
        match = tv_matcher.find_best_match(ep)
        assert match is not None
        assert match.get("episode_missing") is True
        assert match["episode_title"] is None


# ---------------------------------------------------------------------------
# End-to-end apply / undo through TvLibrary
# ---------------------------------------------------------------------------

@requires_ffmpeg
class TestTvApplyUndo:
    def _mp4_episode(self, lib):
        return [e for e in lib.episodes_payload() if e["filename"].endswith(".mp4")][0]

    def test_scan_parses_episodes(self, tv_dir):
        from metamatch import TvLibrary
        lib = TvLibrary()
        lib.scan(str(tv_dir))
        payload = lib.episodes_payload()
        assert any(e["parsed"] and e["ext"] == ".mkv" for e in payload)
        assert any(e["parsed"] and e["ext"] == ".mp4" for e in payload)

    def test_apply_renames_and_writes_sidecars(self, tv_dir, mock_tv_match, mock_thumb_download):
        from metamatch import TvLibrary
        lib = TvLibrary()
        lib.scan(str(tv_dir))
        lib.match()
        target = self._mp4_episode(lib)

        result = lib.apply(target["id"], do_tag=True, do_rename=True, do_nfo=True, do_thumb=True)
        assert not result["error"]
        assert result["renamed"]
        new = result["new_path"]
        assert os.path.exists(new)
        # Plex/Kodi naming: "Show - SxxEyy - Title.ext"
        assert " - S02E05 - " in os.path.basename(new)
        assert os.path.exists(os.path.splitext(new)[0] + ".nfo")
        assert os.path.exists(os.path.splitext(new)[0] + "-thumb.jpg")

    def test_nfo_contains_episode_details(self, tv_dir, mock_tv_match, mock_thumb_download):
        import xml.etree.ElementTree as ET
        from metamatch import TvLibrary
        lib = TvLibrary()
        lib.scan(str(tv_dir))
        lib.match()
        target = self._mp4_episode(lib)
        result = lib.apply(target["id"], do_tag=False, do_rename=False, do_nfo=True, do_thumb=False)

        root = ET.parse(result["nfo_path"]).getroot()
        assert root.tag == "episodedetails"
        assert root.findtext("season") == "2"
        assert root.findtext("episode") == "5"
        assert root.findtext("showtitle")

    def test_mp4_embeds_tv_atoms(self, tv_dir, mock_tv_match):
        from mutagen.mp4 import MP4
        from metamatch import TvLibrary
        lib = TvLibrary()
        lib.scan(str(tv_dir))
        lib.match()
        target = self._mp4_episode(lib)
        result = lib.apply(target["id"], do_tag=True, do_rename=False, do_nfo=False, do_thumb=False)

        tags = MP4(result["new_path"])
        assert tags.get("tvsh")            # show
        assert tags.get("tvsn") == [2]     # season
        assert tags.get("tves") == [5]     # episode
        assert tags.get("stik") == [10]    # "TV Show" media kind

    def test_undo_restores_original(self, tv_dir, mock_tv_match, mock_thumb_download):
        from metamatch import TvLibrary
        lib = TvLibrary()
        lib.scan(str(tv_dir))
        lib.match()
        target = self._mp4_episode(lib)
        original_path = target["path"]

        applied = lib.apply(target["id"], do_tag=True, do_rename=True, do_nfo=True, do_thumb=True)
        new_path = applied["new_path"]
        assert new_path != original_path

        undo = lib.undo(new_path)
        assert not undo["error"]
        assert os.path.exists(original_path)                     # name restored
        assert not os.path.exists(os.path.splitext(new_path)[0] + ".nfo")     # fresh nfo removed
        assert not os.path.exists(os.path.splitext(new_path)[0] + "-thumb.jpg")


# ---------------------------------------------------------------------------
# Failure rollback (reuses the journal/rollback machinery)
# ---------------------------------------------------------------------------

@requires_ffmpeg
class TestTvRollback:
    def _boom(self, *a, **k):
        raise OSError("simulated failure during rename")

    def _mp4_episode(self, lib):
        return [e for e in lib.episodes_payload() if e["filename"].endswith(".mp4")][0]

    def test_rename_failure_rolls_back_sidecars(self, tv_dir, mock_tv_match, mock_thumb_download, monkeypatch):
        from metamatch import TvLibrary
        import metamatch.tv_tagger as tv_tagger_module

        lib = TvLibrary()
        lib.scan(str(tv_dir))
        lib.match()
        target = self._mp4_episode(lib)
        base = os.path.splitext(target["path"])[0]

        monkeypatch.setattr(tv_tagger_module, "rename_to_match", self._boom)
        result = lib.apply(target["id"], do_tag=True, do_rename=True, do_nfo=True, do_thumb=True)

        assert result["error"]
        assert result["rolled_back"] is True
        assert result["recovery_required"] is False
        assert os.path.exists(target["path"])                 # never renamed
        assert not os.path.exists(base + ".nfo")              # fresh nfo removed on rollback
        assert not os.path.exists(base + "-thumb.jpg")        # fresh thumb removed on rollback
        assert lib.journal.get(result["txn_id"]).status == ROLLED_BACK

    def test_failed_compensation_flags_recovery(self, tv_dir, mock_tv_match, mock_thumb_download, monkeypatch):
        from metamatch import TvLibrary
        import metamatch.tv_tagger as tv_tagger_module
        import metamatch.library as library_module

        lib = TvLibrary()
        lib.scan(str(tv_dir))
        lib.match()
        target = self._mp4_episode(lib)

        monkeypatch.setattr(tv_tagger_module, "rename_to_match", self._boom)
        # Make the nfo cleanup step fail too, so rollback can't fully restore.
        real_remove = os.remove

        def selective_remove(p, *a, **k):
            if p.endswith(".nfo"):
                raise OSError("cannot remove nfo")
            return real_remove(p, *a, **k)
        monkeypatch.setattr(library_module.os, "remove", selective_remove)

        result = lib.apply(target["id"], do_tag=False, do_rename=True, do_nfo=True, do_thumb=False)
        assert result["error"]
        assert result["recovery_required"] is True
        assert lib.journal.get(result["txn_id"]).status == RECOVERY_REQUIRED


# ---------------------------------------------------------------------------
# Flask /api/tv/* routes
# ---------------------------------------------------------------------------

@requires_ffmpeg
class TestTvApi:
    def test_scan_and_list(self, app_client, tv_dir):
        resp = app_client.post("/api/tv/scan", json={"folder": str(tv_dir)})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["count"] >= 2
        listing = app_client.get("/api/tv").get_json()
        assert listing["folder"] == str(tv_dir)

    def test_apply_via_api(self, app_client, tv_dir, mock_tv_match, mock_thumb_download):
        app_client.post("/api/tv/scan", json={"folder": str(tv_dir)})
        import app as app_module
        app_module.tv_library.match()
        mp4 = [e for e in app_module.tv_library.episodes_payload() if e["filename"].endswith(".mp4")][0]

        resp = app_client.post("/api/tv/apply", json={"id": mp4["id"], "tag": True, "rename": True})
        assert resp.status_code == 200
        assert not resp.get_json()["error"]

    def test_export_csv_has_tv_columns(self, app_client, tv_dir):
        app_client.post("/api/tv/scan", json={"folder": str(tv_dir)})
        resp = app_client.get("/api/tv/export_csv")
        assert resp.status_code == 200
        header = resp.data.decode("utf-8").splitlines()[0]
        assert "season" in header and "episode" in header and "matched_series" in header


class TestTvUiWiring:
    """The TV tab must actually be present in the served page and wired to
    the /api/tv/* routes - a guard against the template/JS drifting from the
    backend."""

    def test_index_has_tv_tab_and_controls(self, app_client):
        html = app_client.get("/").data.decode("utf-8")
        assert 'data-view="tv"' in html
        assert 'id="tvView"' in html
        assert 'id="tvScanBtn"' in html
        assert 'id="tvTableBody"' in html

    def test_app_js_references_tv_endpoints(self, app_client):
        js = app_client.get("/static/app.js").data.decode("utf-8")
        for endpoint in ("/api/tv/scan", "/api/tv/match/start", "/api/tv/apply",
                         "/api/tv/undo", "/api/tv/export_csv",
                         "/api/tv/series_metadata"):
            assert endpoint in js

    def test_index_has_series_metadata_button(self, app_client):
        html = app_client.get("/").data.decode("utf-8")
        assert 'id="tvSeriesMetaBtn"' in html


# ---------------------------------------------------------------------------
# Series-level metadata (tvshow.nfo + series/season posters at the show root)
# ---------------------------------------------------------------------------

@requires_ffmpeg
class TestSeriesMetadata:
    def _series_root(self, tv_dir):
        # The mkv lives in "Test Show/Season 01/"; the show root is "Test Show".
        return os.path.join(str(tv_dir), "Test Show")

    def test_writes_tvshow_nfo_and_posters(self, tv_dir, mock_tv_match, mock_tv_series_details):
        from metamatch import TvLibrary
        lib = TvLibrary()
        lib.scan(str(tv_dir))
        lib.match()

        summary = lib.write_series_metadata(min_confidence=0)
        assert summary["succeeded"] >= 1

        root = self._series_root(tv_dir)
        assert os.path.exists(os.path.join(root, "tvshow.nfo"))
        assert os.path.exists(os.path.join(root, "poster.jpg"))
        assert os.path.exists(os.path.join(root, "season01-poster.jpg"))

    def test_tvshow_nfo_contents(self, tv_dir, mock_tv_match, mock_tv_series_details):
        import xml.etree.ElementTree as ET
        from metamatch import TvLibrary
        lib = TvLibrary()
        lib.scan(str(tv_dir))
        lib.match()
        lib.write_series_metadata(min_confidence=0)

        root = ET.parse(os.path.join(self._series_root(tv_dir), "tvshow.nfo")).getroot()
        assert root.tag == "tvshow"
        assert root.findtext("title") == "Breaking Bad"
        assert root.findtext("premiered") == "2008-01-20"
        assert {g.text for g in root.findall("genre")} == {"Drama", "Crime"}
        assert root.find("uniqueid").text  # tmdb id present

    def test_series_root_resolves_past_season_folder(self, tv_dir):
        from metamatch import TvLibrary
        lib = TvLibrary()
        mkv = os.path.join(str(tv_dir), "Test Show", "Season 01", "Test.Show.S01E02.The.Test.720p.WEB-DL.mkv")
        assert lib._series_root_for(mkv) == os.path.join(str(tv_dir), "Test Show")

    def test_undo_removes_created_series_files(self, tv_dir, mock_tv_match, mock_tv_series_details):
        from metamatch import TvLibrary
        lib = TvLibrary()
        lib.scan(str(tv_dir))
        lib.match()
        lib.write_series_metadata(min_confidence=0)
        root = self._series_root(tv_dir)
        assert os.path.exists(os.path.join(root, "tvshow.nfo"))

        undo = lib.undo_series_metadata_all()
        assert undo["restored"] >= 1
        assert not os.path.exists(os.path.join(root, "tvshow.nfo"))
        assert not os.path.exists(os.path.join(root, "poster.jpg"))
        assert not os.path.exists(os.path.join(root, "season01-poster.jpg"))

    def test_undo_restores_preexisting_tvshow_nfo(self, tv_dir, mock_tv_match, mock_tv_series_details):
        from metamatch import TvLibrary
        root = self._series_root(tv_dir)
        os.makedirs(root, exist_ok=True)
        original = os.path.join(root, "tvshow.nfo")
        with open(original, "wb") as f:
            f.write(b"<tvshow><title>My Hand-Edited Version</title></tvshow>")

        lib = TvLibrary()
        lib.scan(str(tv_dir))
        lib.match()
        lib.write_series_metadata(min_confidence=0)
        assert b"Breaking Bad" in open(original, "rb").read()  # overwritten by apply

        lib.undo_series_metadata_all()
        assert open(original, "rb").read() == b"<tvshow><title>My Hand-Edited Version</title></tvshow>"

    def test_write_failure_rolls_back(self, tv_dir, mock_tv_match, mock_tv_series_details, monkeypatch):
        from metamatch import TvLibrary
        import metamatch.tv_tagger as tv_tagger_module
        from metamatch.journal import ROLLED_BACK

        lib = TvLibrary()
        lib.scan(str(tv_dir))
        lib.match()
        root = self._series_root(tv_dir)

        # nfo writes fine, then poster download raises mid-write.
        def boom(url, dest):
            raise OSError("disk full writing poster")
        monkeypatch.setattr(tv_tagger_module, "download_image", boom)

        summary = lib.write_series_metadata(min_confidence=0)
        r = summary["results"][0]
        assert r["error"]
        assert r["rolled_back"] is True
        # the tvshow.nfo written before the failure was rolled back (deleted)
        assert not os.path.exists(os.path.join(root, "tvshow.nfo"))
        assert lib.journal.get(r["txn_id"]).status == ROLLED_BACK


@requires_ffmpeg
class TestSeriesMetadataApi:
    def test_series_metadata_via_api(self, app_client, tv_dir, mock_tv_match, mock_tv_series_details):
        app_client.post("/api/tv/scan", json={"folder": str(tv_dir)})
        import app as app_module
        app_module.tv_library.match()

        resp = app_client.post("/api/tv/series_metadata", json={"min_confidence": 0})
        assert resp.status_code == 200
        assert resp.get_json()["succeeded"] >= 1

        undo = app_client.post("/api/tv/series_metadata/undo")
        assert undo.status_code == 200
        assert undo.get_json()["restored"] >= 1
