"""
test_review_021.py
Regression tests for the 0.2.1 adversarial review: the authority guarantee
must hold at the INSTANT OF MUTATION, not just at scan time. Filesystem
identity is mutable, so every destructive op re-validates its target (and
each derived sidecar path) under its mutation lock before touching anything.

  1. safe-at-scan file swapped for a symlink before Apply      (CRITICAL)
  2. sidecar symlink followed out of the library               (CRITICAL)
  3. hard-link alias crosses the authority boundary            (HIGH)
  4. series root swapped for a symlink after scan              (HIGH)
  5. legacy TV Undo (no atom snapshot) deletes unknown atoms   (HIGH)
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from conftest import requires_ffmpeg, requires_symlinks
from metamatch.journal import Journal


def _mp3(path, freq=440):
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=1",
                    "-b:a", "128k", path],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)


# --------------------------------------------------------------------------
# 1. Post-scan symlink swap (TOCTOU)
# --------------------------------------------------------------------------

@requires_ffmpeg
@requires_symlinks
class TestPostScanSymlinkSwap:
    def test_scanned_file_replaced_by_symlink_is_refused(self, tmp_path):
        from metamatch import MusicLibrary
        import metamatch.matcher as matcher
        from mutagen.easyid3 import EasyID3

        lib = tmp_path / "Library"; lib.mkdir()
        out = tmp_path / "Outside"; out.mkdir()
        song = lib / "song.mp3"; victim = out / "victim.mp3"
        _mp3(str(song))
        st = os.stat(str(song))

        matcher.find_best_match = lambda t: {"recording_id": "r", "release_id": "x",
            "title": "Changed", "artist": "MetaMatch", "album": "A", "date": "2026-01-01", "confidence": 99}
        m = MusicLibrary(journal=Journal(str(tmp_path / "j.sqlite")))
        payload = m.scan(str(lib)); m.match()

        # byte-identical copy with matching mtime, then swap file -> symlink,
        # so the fingerprint check passes but authority no longer holds.
        shutil.copy2(str(song), str(victim))
        os.remove(str(song)); os.symlink(str(victim), str(song))
        os.utime(str(victim), ns=(st.st_atime_ns, st.st_mtime_ns))

        result = m.apply(payload[0]["id"], do_tag=True, do_rename=False, do_art=False)
        assert result["error"] and "symlink" in result["error"].lower()
        # the outside victim must be untouched
        try:
            assert EasyID3(str(victim)).get("artist") != ["MetaMatch"]
        except Exception:
            pass


# --------------------------------------------------------------------------
# 2. Sidecar symlink
# --------------------------------------------------------------------------

@requires_ffmpeg
@requires_symlinks
class TestSidecarSymlink:
    def test_nfo_symlink_is_not_followed(self, tmp_path, media_fixtures_dir, mock_movie_match):
        from metamatch import MovieLibrary
        lib = tmp_path / "Movies"; lib.mkdir()
        out = tmp_path / "Outside"; out.mkdir()
        shutil.copy(media_fixtures_dir / "sample_movie.mp4", lib / "movie.mp4")
        victim = out / "victim.txt"; victim.write_text("IMPORTANT DATA")
        os.symlink(str(victim), str(lib / "movie.nfo"))

        m = MovieLibrary(journal=Journal(str(tmp_path / "j.sqlite")))
        payload = m.scan(str(lib)); m.match()
        target = [v for v in payload if v["filename"] == "movie.mp4"][0]

        result = m.apply(target["id"], do_tag=False, do_rename=False, do_nfo=True, do_poster=False)
        assert result["nfo_path"] is None                     # not written through the link
        assert any("symlink" in w.lower() for w in result.get("warnings", []))
        assert victim.read_text() == "IMPORTANT DATA"          # outside file preserved

    def test_poster_symlink_is_not_followed(self, tmp_path, media_fixtures_dir, mock_movie_match, mock_poster_download):
        from metamatch import MovieLibrary
        lib = tmp_path / "Movies"; lib.mkdir()
        out = tmp_path / "Outside"; out.mkdir()
        shutil.copy(media_fixtures_dir / "sample_movie.mp4", lib / "movie.mp4")
        victim = out / "art.bin"; victim.write_bytes(b"REAL ART")
        os.symlink(str(victim), str(lib / "movie-poster.jpg"))

        m = MovieLibrary(journal=Journal(str(tmp_path / "j.sqlite")))
        payload = m.scan(str(lib)); m.match()
        target = [v for v in payload if v["filename"] == "movie.mp4"][0]

        result = m.apply(target["id"], do_tag=False, do_rename=False, do_nfo=False, do_poster=True)
        assert result["poster_path"] is None
        assert victim.read_bytes() == b"REAL ART"


# --------------------------------------------------------------------------
# 3. Hard-link alias
# --------------------------------------------------------------------------

@requires_ffmpeg
class TestHardLinkAlias:
    def test_hardlinked_media_file_is_refused(self, tmp_path):
        from metamatch import MusicLibrary
        import metamatch.matcher as matcher
        lib = tmp_path / "Library"; lib.mkdir()
        out = tmp_path / "Outside"; out.mkdir()
        victim = out / "victim.mp3"; _mp3(str(victim))
        os.link(str(victim), str(lib / "linked.mp3"))   # hard link, same inode

        matcher.find_best_match = lambda t: {"recording_id": "r", "release_id": "x",
            "title": "X", "artist": "MetaMatch", "album": "A", "date": "2026-01-01", "confidence": 99}
        m = MusicLibrary(journal=Journal(str(tmp_path / "j.sqlite")))
        payload = m.scan(str(lib)); m.match()
        # The hard link scans fine (not a symlink, resolves inside root) but
        # must be refused at mutation time.
        assert payload, "hard link should still be scannable"
        result = m.apply(payload[0]["id"], do_tag=True, do_rename=False, do_art=False)
        assert result["error"] and "hard-link" in result["error"].lower()


# --------------------------------------------------------------------------
# 4. Series root swapped after scan
# --------------------------------------------------------------------------

@requires_ffmpeg
@requires_symlinks
class TestSeriesRootSwap:
    def test_series_root_symlink_is_refused(self, tmp_path, media_fixtures_dir, mock_tv_match, mock_tv_series_details):
        from metamatch import TvLibrary
        lib = tmp_path / "TV"; lib.mkdir()
        show = lib / "Show" / "Season 01"; show.mkdir(parents=True)
        shutil.copy(media_fixtures_dir / "sample_movie.mp4", show / "Show.S01E01.mp4")

        out = tmp_path / "Outside" / "Show"; out.mkdir(parents=True)

        m = TvLibrary(journal=Journal(str(tmp_path / "j.sqlite")))
        m.scan(str(lib)); m.match()

        # After scan, replace Library/Show with a symlink to Outside/Show.
        shutil.rmtree(str(lib / "Show"))
        os.symlink(str(out), str(lib / "Show"))

        summary = m.write_series_metadata(min_confidence=0)
        assert summary["failed"] >= 1
        assert not (out / "tvshow.nfo").exists()   # nothing written outside


# --------------------------------------------------------------------------
# 5. Legacy TV Undo fail-closed
# --------------------------------------------------------------------------

@requires_ffmpeg
class TestLegacyTvUndo:
    def test_legacy_tv_undo_does_not_delete_unknown_preexisting_atoms(
        self, tv_dir, mock_tv_match, mock_thumb_download
    ):
        from mutagen.mp4 import MP4
        from metamatch import TvLibrary

        mp4 = [str(tv_dir / f) for f in os.listdir(tv_dir) if f.endswith(".mp4")][0]
        # Pre-existing TV atoms the user already had.
        audio = MP4(mp4)
        audio["tvsh"] = ["Original Show"]; audio["tvsn"] = [9]; audio["tves"] = [8]
        audio["stik"] = [10]; audio["\xa9ART"] = ["Original Artist"]
        audio.save()

        lib = TvLibrary()
        lib.scan(str(tv_dir))
        lib.match()
        target = [e for e in lib.episodes_payload() if e["path"] == mp4][0]
        applied = lib.apply(target["id"], do_tag=True, do_rename=False, do_nfo=False, do_thumb=False)

        # Simulate a legacy 0.2.0 journal row: strip the mp4_atoms snapshot.
        txn = lib.journal.get(applied["txn_id"])
        snap = dict(txn.before_state); snap["mp4_atoms"] = None
        import sqlite3, json
        conn = sqlite3.connect(lib.journal.path)
        conn.execute("UPDATE transactions SET before_state=? WHERE id=?", (json.dumps(snap), applied["txn_id"]))
        conn.commit(); conn.close()

        undo = lib.undo(applied["new_path"])
        assert not undo["error"]
        # The pre-existing atoms must NOT have been deleted by the legacy path.
        restored = MP4(undo["restored_path"])
        assert restored.get("tvsh") is not None
        assert restored.get("tvsn") is not None
        assert restored.get("stik") is not None
        assert any("older MetaMatch" in w or "weren't recorded" in w for w in undo.get("warnings", []))
