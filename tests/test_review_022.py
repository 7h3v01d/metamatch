"""
test_review_022.py
Regression tests for the 0.2.2 adversarial review: restart Undo must apply the
SAME filesystem-authority validation as Apply, even though it runs with no
active scan root. The library root that authorised each operation is now
persisted in its journal transaction, so restart recovery re-validates against
it; a legacy row without a recorded root falls back to a check that still
enforces every root-independent property (links AND hard links).

  BLOCKER: restart Undo bypassed the hard-link check via the no-root fallback.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from conftest import requires_ffmpeg
from metamatch.journal import Journal


def _mp3(path, freq=440):
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=1",
                    "-b:a", "128k", path],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)


class TestJournalRecordsLibraryRoot:
    def test_begin_persists_library_root(self, tmp_path):
        jr = Journal(str(tmp_path / "j.sqlite"))
        tid = jr.begin("music", "/lib/a.mp3", "/lib/a.mp3", {}, {}, library_root="/lib")
        assert jr.get(tid).library_root == "/lib"

    def test_missing_library_root_is_none(self, tmp_path):
        jr = Journal(str(tmp_path / "j.sqlite"))
        tid = jr.begin("music", "/lib/a.mp3", "/lib/a.mp3", {}, {})
        assert jr.get(tid).library_root is None


@requires_ffmpeg
class TestRestartUndoHardlinkAuthority:
    """The reproduced blocker and its siblings across the three media types."""

    def _apply_then_restart(self, LibraryCls, kind_dir, journal_path, target_pred):
        lib = LibraryCls(journal=Journal(journal_path))
        payload = lib.scan(str(kind_dir))
        lib.match()
        target = [x for x in payload if target_pred(x)][0]
        applied = lib.apply(target["id"], do_tag=True, do_rename=False)
        # restart: brand-new library on the same journal, no scan (folder=None)
        restarted = LibraryCls(journal=Journal(journal_path))
        assert restarted.folder is None
        return restarted, applied["new_path"]

    def test_restart_music_undo_refuses_hardlinked_current_file(self, tmp_path):
        from metamatch import MusicLibrary
        import metamatch.matcher as matcher
        from mutagen.easyid3 import EasyID3

        lib_dir = tmp_path / "Library"; lib_dir.mkdir()
        out = tmp_path / "Outside"; out.mkdir()
        _mp3(str(lib_dir / "song.mp3"))
        matcher.find_best_match = lambda t: {"recording_id": "r", "release_id": "x",
            "title": "New", "artist": "MetaMatch", "album": "A", "date": "2026-01-01", "confidence": 99}
        jp = str(tmp_path / "j.sqlite")

        m = MusicLibrary(journal=Journal(jp)); p = m.scan(str(lib_dir)); m.match()
        applied = m.apply(p[0]["id"], do_tag=True, do_rename=False, do_art=False)
        current = applied["new_path"]

        restarted = MusicLibrary(journal=Journal(jp))
        assert restarted.folder is None
        # replace current media with a hard link to a byte-identical outside file
        victim = out / "victim.mp3"; shutil.copy2(current, str(victim))
        os.remove(current); os.link(str(victim), current)

        result = restarted.undo(current)
        assert result["error"] and "hard-link" in result["error"].lower()
        # outside file must be untouched (still the applied tags)
        assert EasyID3(str(victim)).get("artist") == ["MetaMatch"]

    def test_restart_music_undo_refuses_symlinked_current_file(self, tmp_path):
        from metamatch import MusicLibrary
        import metamatch.matcher as matcher
        lib_dir = tmp_path / "Library"; lib_dir.mkdir()
        out = tmp_path / "Outside"; out.mkdir()
        _mp3(str(lib_dir / "song.mp3"))
        matcher.find_best_match = lambda t: {"recording_id": "r", "release_id": "x",
            "title": "New", "artist": "MetaMatch", "album": "A", "date": "2026-01-01", "confidence": 99}
        jp = str(tmp_path / "j.sqlite")

        m = MusicLibrary(journal=Journal(jp)); p = m.scan(str(lib_dir)); m.match()
        applied = m.apply(p[0]["id"], do_tag=True, do_rename=False, do_art=False)
        current = applied["new_path"]

        restarted = MusicLibrary(journal=Journal(jp))
        victim = out / "victim.mp3"; shutil.copy2(current, str(victim))
        os.remove(current); os.symlink(str(victim), current)

        result = restarted.undo(current)
        assert result["error"] and ("symlink" in result["error"].lower() or "reparse" in result["error"].lower())

    def test_restart_movie_undo_refuses_hardlink(self, tmp_path, media_fixtures_dir, mock_movie_match):
        from metamatch import MovieLibrary
        lib_dir = tmp_path / "Movies"; lib_dir.mkdir()
        out = tmp_path / "Outside"; out.mkdir()
        shutil.copy(media_fixtures_dir / "sample_movie.mp4", lib_dir / "movie.mp4")
        jp = str(tmp_path / "j.sqlite")

        m = MovieLibrary(journal=Journal(jp)); p = m.scan(str(lib_dir)); m.match()
        target = [v for v in p if v["filename"] == "movie.mp4"][0]
        applied = m.apply(target["id"], do_tag=True, do_rename=False, do_nfo=False, do_poster=False)
        current = applied["new_path"]

        restarted = MovieLibrary(journal=Journal(jp))
        victim = out / "victim.mp4"; shutil.copy2(current, str(victim))
        os.remove(current); os.link(str(victim), current)

        result = restarted.undo(current)
        assert result["error"] and "hard-link" in result["error"].lower()

    def test_restart_tv_undo_refuses_hardlink(self, tmp_path, media_fixtures_dir, mock_tv_match, mock_thumb_download):
        from metamatch import TvLibrary
        lib_dir = tmp_path / "TV"; lib_dir.mkdir()
        out = tmp_path / "Outside"; out.mkdir()
        shutil.copy(media_fixtures_dir / "sample_movie.mp4", lib_dir / "Show.S01E01.mp4")
        jp = str(tmp_path / "j.sqlite")

        m = TvLibrary(journal=Journal(jp)); p = m.scan(str(lib_dir)); m.match()
        target = [e for e in p if e["filename"].endswith(".mp4")][0]
        applied = m.apply(target["id"], do_tag=True, do_rename=False, do_nfo=False, do_thumb=False)
        current = applied["new_path"]

        restarted = TvLibrary(journal=Journal(jp))
        victim = out / "victim.mp4"; shutil.copy2(current, str(victim))
        os.remove(current); os.link(str(victim), current)

        result = restarted.undo(current)
        assert result["error"] and "hard-link" in result["error"].lower()

    def test_legacy_row_without_root_still_refuses_hardlink(self, tmp_path):
        """A transaction from before library_root was recorded must still be
        protected by the hardened no-root fallback."""
        import sqlite3
        from metamatch import MusicLibrary
        import metamatch.matcher as matcher
        lib_dir = tmp_path / "Library"; lib_dir.mkdir()
        out = tmp_path / "Outside"; out.mkdir()
        _mp3(str(lib_dir / "song.mp3"))
        matcher.find_best_match = lambda t: {"recording_id": "r", "release_id": "x",
            "title": "New", "artist": "MetaMatch", "album": "A", "date": "2026-01-01", "confidence": 99}
        jp = str(tmp_path / "j.sqlite")

        m = MusicLibrary(journal=Journal(jp)); p = m.scan(str(lib_dir)); m.match()
        applied = m.apply(p[0]["id"], do_tag=True, do_rename=False, do_art=False)
        current = applied["new_path"]
        # simulate a legacy row: strip the recorded root
        conn = sqlite3.connect(jp); conn.execute("UPDATE transactions SET library_root=NULL"); conn.commit(); conn.close()

        restarted = MusicLibrary(journal=Journal(jp))
        victim = out / "victim.mp3"; shutil.copy2(current, str(victim))
        os.remove(current); os.link(str(victim), current)

        result = restarted.undo(current)
        assert result["error"] and "hard-link" in result["error"].lower()
