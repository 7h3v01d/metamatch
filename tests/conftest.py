"""
conftest.py
Shared fixtures for the MetaMatch test suite.

Media fixtures (mp3/wma/flac/mp4/mkv) are generated once per test session
with ffmpeg into a session-scoped temp directory, then copied into a
fresh per-test directory so tests can rename/tag/delete them freely
without interfering with each other. Tests that need real media and find
ffmpeg unavailable are skipped rather than failed, since ffmpeg is a
system dependency this project doesn't (and can't) vendor.

Network-touching code (MusicBrainz, TMDB, Cover Art Archive, poster
downloads) is never hit for real in this suite - fixtures monkeypatch
the relevant `find_best_match` / `fetch_cover_art` / `download_poster`
functions so tests stay fast, deterministic, and don't require internet
access or API keys to run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None
requires_ffmpeg = pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="ffmpeg not installed on this machine")


# ---------------------------------------------------------------------------
# Media fixture generation (session-scoped: built once, copied per test)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def media_fixtures_dir(tmp_path_factory):
    """Builds one canonical copy of each test media file for the whole session."""
    base = tmp_path_factory.mktemp("media_fixtures")

    if not FFMPEG_AVAILABLE:
        return base

    def run(*args):
        subprocess.run(list(args), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=60)

    # Plain untagged mp3 with a "messy" scene-release-style filename, for
    # testing filename-parsing fallback when no tags are present.
    run("ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-ar", "44100", "-ac", "2", "-b:a", "128k",
        str(base / "01 - Test Artist - Test Song (Official Audio).mp3"))

    # Same audio, but we'll tag it with mutagen after ffmpeg writes it.
    run("ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-ar", "44100", "-ac", "2", "-b:a", "128k",
        str(base / "tagged.mp3"))
    from mutagen.easyid3 import EasyID3
    from mutagen.id3 import ID3NoHeaderError
    path = str(base / "tagged.mp3")
    try:
        tags = EasyID3(path)
    except ID3NoHeaderError:
        tags = EasyID3()
        tags.save(path)
        tags = EasyID3(path)
    tags["artist"] = "Radiohead"
    tags["title"] = "Karma Police"
    tags["album"] = "OK Computer"
    tags["date"] = "1997"
    tags.save(path)

    run("ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=220:duration=2",
        str(base / "sample.wma"))

    run("ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=330:duration=1",
        "-c:a", "flac", str(base / "sample.flac"))

    run("ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(base / "sample_movie.mp4"))

    run("ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=320x240:d=2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(base / "Test.Movie.2020.1080p.BluRay.x264-GROUP.mkv"))

    return base


@pytest.fixture
def music_dir(tmp_path, media_fixtures_dir):
    """A fresh folder containing one tagged and one untagged/messy-named mp3."""
    dest = tmp_path / "music"
    dest.mkdir()
    if FFMPEG_AVAILABLE:
        shutil.copy(media_fixtures_dir / "tagged.mp3", dest / "tagged.mp3")
        shutil.copy(
            media_fixtures_dir / "01 - Test Artist - Test Song (Official Audio).mp3",
            dest / "01 - Test Artist - Test Song (Official Audio).mp3",
        )
    return dest


@pytest.fixture
def movie_dir(tmp_path, media_fixtures_dir):
    """A fresh folder containing one mp4 and one scene-release-named mkv."""
    dest = tmp_path / "movies"
    dest.mkdir()
    if FFMPEG_AVAILABLE:
        shutil.copy(media_fixtures_dir / "sample_movie.mp4", dest / "sample_movie.mp4")
        shutil.copy(
            media_fixtures_dir / "Test.Movie.2020.1080p.BluRay.x264-GROUP.mkv",
            dest / "Test.Movie.2020.1080p.BluRay.x264-GROUP.mkv",
        )
    return dest


# ---------------------------------------------------------------------------
# Config isolation (never touch the real ~/.metamatch/config.json)
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    from core import config as app_config

    fake_dir = tmp_path / "config_home"
    fake_path = fake_dir / "config.json"
    monkeypatch.setattr(app_config, "CONFIG_DIR", str(fake_dir))
    monkeypatch.setattr(app_config, "CONFIG_PATH", str(fake_path))
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    return app_config


# ---------------------------------------------------------------------------
# Network mocks
# ---------------------------------------------------------------------------

def make_fake_music_match(**overrides) -> dict:
    base = {
        "recording_id": "rec-1", "release_id": "rel-1",
        "title": "Karma Police", "artist": "Radiohead", "album": "OK Computer",
        "date": "1997-01-01", "length_ms": 200000, "mb_score": 95,
        "title_similarity": 95, "artist_similarity": 95, "duration_similarity": 90,
        "confidence": 92.5, "musicbrainz_url": "https://musicbrainz.org/recording/rec-1",
    }
    base.update(overrides)
    return base


def make_fake_movie_match(**overrides) -> dict:
    base = {
        "tmdb_id": 603, "title": "Test Movie", "original_title": None, "year": "2020",
        "release_date": "2020-01-01", "overview": "A test movie.", "vote_average": 8.0,
        "poster_path": "/poster.jpg",
        "poster_url": "https://image.tmdb.org/t/p/w342/poster.jpg",
        "poster_url_full": "https://image.tmdb.org/t/p/original/poster.jpg",
        "title_similarity": 95, "year_similarity": 100, "confidence": 92.0,
        "tmdb_url": "https://www.themoviedb.org/movie/603",
    }
    base.update(overrides)
    return base


@pytest.fixture
def mock_music_match(monkeypatch):
    """Patches MusicBrainz lookups everywhere they're called from (core.matcher and app)."""
    import core.matcher as matcher_module

    def fake_find_best_match(track):
        return make_fake_music_match()

    monkeypatch.setattr(matcher_module, "find_best_match", fake_find_best_match)
    try:
        import app as app_module
        monkeypatch.setattr(app_module, "match_tracks", matcher_module.match_tracks)
    except ImportError:
        pass
    return fake_find_best_match


@pytest.fixture
def mock_movie_match(monkeypatch):
    """Patches TMDB lookups everywhere they're called from (core.movie_matcher)."""
    import core.movie_matcher as movie_matcher_module

    def fake_find_best_match(video):
        return make_fake_movie_match()

    monkeypatch.setattr(movie_matcher_module, "find_best_match", fake_find_best_match)
    return fake_find_best_match


@pytest.fixture
def mock_cover_art(monkeypatch):
    """Patches Cover Art Archive fetches, including the copy already bound into app.py."""
    fake_bytes = b"\xff\xd8\xff\xe0FAKEJPEGDATA" * 5

    def fake_fetch(release_id, size="250"):
        if not release_id:
            return None
        return (fake_bytes, "image/jpeg")

    import core.art as art_module
    monkeypatch.setattr(art_module, "fetch_cover_art", fake_fetch)
    try:
        import app as app_module
        monkeypatch.setattr(app_module, "fetch_cover_art", fake_fetch)
    except ImportError:
        pass
    return fake_bytes


@pytest.fixture
def mock_poster_download(monkeypatch):
    """Patches poster downloads (movie_tagger writes a fake poster file instead of hitting TMDB's CDN)."""
    import core.movie_tagger as movie_tagger_module
    fake_bytes = b"FAKEPOSTERBYTES"

    def fake_download(path, match):
        base = os.path.splitext(path)[0]
        dest = base + "-poster.jpg"
        with open(dest, "wb") as f:
            f.write(fake_bytes)
        return dest

    monkeypatch.setattr(movie_tagger_module, "download_poster", fake_download)
    return fake_bytes


# ---------------------------------------------------------------------------
# Flask app client with isolated, reset in-memory state per test
# ---------------------------------------------------------------------------

@pytest.fixture
def app_client(isolated_config, monkeypatch):
    import app as app_module

    app_module.STATE["folder"] = None
    app_module.STATE["tracks"] = {}
    app_module.STATE["order"] = []
    app_module.STATE["match_progress"] = {"running": False, "done": 0, "total": 0}
    app_module.STATE["undo_by_path"] = {}

    app_module.MOVIE_STATE["folder"] = None
    app_module.MOVIE_STATE["videos"] = {}
    app_module.MOVIE_STATE["order"] = []
    app_module.MOVIE_STATE["match_progress"] = {"running": False, "done": 0, "total": 0, "error": None}
    app_module.MOVIE_STATE["undo_by_path"] = {}

    app_module.app.config.update(TESTING=True)
    return app_module.app.test_client()


@pytest.fixture
def wait_for_progress():
    """Returns a helper that polls a Flask progress endpoint until matching finishes."""
    import time

    def _wait(client, url, timeout=10):
        deadline = time.time() + timeout
        progress = None
        while time.time() < deadline:
            progress = client.get(url).get_json()
            if not progress["running"]:
                return progress
            time.sleep(0.05)
        raise TimeoutError(f"Timed out waiting for {url} to finish: {progress}")

    return _wait
