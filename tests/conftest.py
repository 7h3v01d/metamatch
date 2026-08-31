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


def _symlinks_supported() -> bool:
    """Whether this process can actually create a symlink. On Windows this
    needs admin rights or Developer Mode (otherwise os.symlink raises
    WinError 1314). Tests that reproduce symlink-authority attacks need to
    CREATE a symlink to set up the scenario, so they skip cleanly where the
    OS won't allow it rather than erroring - the product code being tested
    still runs everywhere; only the adversarial test setup needs the privilege."""
    import tempfile
    d = tempfile.mkdtemp()
    target = os.path.join(d, "t"); link = os.path.join(d, "l")
    try:
        with open(target, "w") as f:
            f.write("x")
        os.symlink(target, link)
        return True
    except (OSError, NotImplementedError, AttributeError):
        return False
    finally:
        shutil.rmtree(d, ignore_errors=True)


SYMLINKS_SUPPORTED = _symlinks_supported()
requires_symlinks = pytest.mark.skipif(
    not SYMLINKS_SUPPORTED,
    reason="creating symlinks isn't permitted here (Windows needs admin/Developer Mode)")


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
    from metamatch import config as app_config

    fake_dir = tmp_path / "config_home"
    fake_path = fake_dir / "config.json"
    monkeypatch.setattr(app_config, "CONFIG_DIR", str(fake_dir))
    monkeypatch.setattr(app_config, "CONFIG_PATH", str(fake_path))
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    return app_config


@pytest.fixture(autouse=True)
def isolated_journal(tmp_path, monkeypatch):
    """Every MusicLibrary()/MovieLibrary() construction opens a journal at
    journal_module.DEFAULT_JOURNAL_PATH unless one is passed explicitly -
    autouse here so no test (there are dozens across several files) needs
    to remember to isolate it individually. Without this, the whole suite
    would read and write the real ~/.metamatch/journal.sqlite."""
    from metamatch import journal as journal_module

    fake_path = str(tmp_path / "isolated_journal.sqlite")
    monkeypatch.setattr(journal_module, "DEFAULT_JOURNAL_PATH", fake_path)
    return fake_path


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
        "margin": None, "ambiguity": "none", "runner_up": None,
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
        "margin": None, "ambiguity": "none", "runner_up": None,
        "tmdb_url": "https://www.themoviedb.org/movie/603",
    }
    base.update(overrides)
    return base


@pytest.fixture
def mock_music_match(monkeypatch):
    """Patches MusicBrainz lookups at the source (metamatch.matcher.find_best_match).

    match_tracks() looks this name up from its own module globals at call
    time, so patching it here covers callers through MusicLibrary, the
    Flask app, or any other code path - without needing to know who's
    calling.
    """
    import metamatch.matcher as matcher_module

    def fake_find_best_match(track):
        return make_fake_music_match()

    monkeypatch.setattr(matcher_module, "find_best_match", fake_find_best_match)
    return fake_find_best_match


@pytest.fixture
def mock_movie_match(monkeypatch):
    """Patches TMDB lookups at the source (metamatch.movie_matcher.find_best_match)."""
    import metamatch.movie_matcher as movie_matcher_module

    def fake_find_best_match(video):
        return make_fake_movie_match()

    monkeypatch.setattr(movie_matcher_module, "find_best_match", fake_find_best_match)
    return fake_find_best_match


@pytest.fixture
def mock_cover_art(monkeypatch):
    """Patches Cover Art Archive fetches, including app.py's directly-imported copy."""
    fake_bytes = b"\xff\xd8\xff\xe0FAKEJPEGDATA" * 5

    def fake_fetch(release_id, size="250"):
        if not release_id:
            return None
        return (fake_bytes, "image/jpeg")

    import metamatch.art as art_module
    monkeypatch.setattr(art_module, "fetch_cover_art", fake_fetch)
    try:
        import app as app_module
        # app.py's /api/art route imported fetch_cover_art by name, so that
        # binding needs patching separately from the module attribute above.
        monkeypatch.setattr(app_module, "fetch_cover_art", fake_fetch)
    except ImportError:
        pass
    return fake_bytes


@pytest.fixture
def mock_poster_download(monkeypatch):
    """Patches poster downloads (movie_tagger writes a fake poster file instead of hitting TMDB's CDN)."""
    import metamatch.movie_tagger as movie_tagger_module
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
# Flask app client with a fresh MusicLibrary/MovieLibrary per test
# ---------------------------------------------------------------------------

@pytest.fixture
def app_client(isolated_config, isolated_journal, monkeypatch):
    import app as app_module
    from metamatch import MusicLibrary, MovieLibrary, TvLibrary

    # Swap in fresh library instances so tests never see state left over
    # from a previous test (mirrors what a host app gets by simply
    # instantiating its own libraries per session). Movie and TV share the
    # music journal, exactly as app.py wires them in production.
    fresh_music = MusicLibrary()
    monkeypatch.setattr(app_module, "music_library", fresh_music)
    monkeypatch.setattr(app_module, "movie_library", MovieLibrary(journal=fresh_music.journal))
    monkeypatch.setattr(app_module, "tv_library", TvLibrary(journal=fresh_music.journal))

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


# ---------------------------------------------------------------------------
# TV fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tv_dir(tmp_path, media_fixtures_dir):
    """A folder of episode files: an mkv (ffmpeg-remux path) inside a
    'Season 01' subfolder, and an mp4 (direct-atom path) named flat."""
    dest = tmp_path / "tv"
    dest.mkdir()
    if FFMPEG_AVAILABLE:
        season = dest / "Test Show" / "Season 01"
        season.mkdir(parents=True)
        shutil.copy(
            media_fixtures_dir / "Test.Movie.2020.1080p.BluRay.x264-GROUP.mkv",
            season / "Test.Show.S01E02.The.Test.720p.WEB-DL.mkv",
        )
        shutil.copy(media_fixtures_dir / "sample_movie.mp4", dest / "Another.Show.S02E05.mp4")
    return dest


def make_fake_tv_match(**overrides) -> dict:
    base = {
        "type": "tv",
        "series_tmdb_id": 1396,
        "series_name": "Breaking Bad",
        "series_year": "2008",
        "season": 1,
        "episode": 2,
        "episodes": [2],
        "episode_title": "Cat's in the Bag...",
        "episode_overview": "Walt and Jesse try to dispose of the bodies.",
        "air_date": "2008-01-27",
        "vote_average": 8.5,
        "still_path": "/still.jpg",
        "still_url": "https://image.tmdb.org/t/p/w300/still.jpg",
        "still_url_full": "https://image.tmdb.org/t/p/original/still.jpg",
        "name_similarity": 95.0,
        "confidence": 96.0,
        "margin": None, "ambiguity": "none", "runner_up": None,
        "tmdb_url": "https://www.themoviedb.org/tv/1396/season/1/episode/2",
    }
    base.update(overrides)
    return base


@pytest.fixture
def mock_tv_match(monkeypatch):
    """Patches TMDB TV lookups at the source (tv_matcher.find_best_match),
    honouring the file's own parsed season/episode so multi-file folders get
    correct per-episode matches."""
    import metamatch.tv_matcher as tv_matcher_module

    def fake_find_best_match(ep_file):
        if not ep_file.parsed:
            return None
        return make_fake_tv_match(
            season=ep_file.season, episode=ep_file.episode,
            episodes=ep_file.episodes or [ep_file.episode],
        )

    monkeypatch.setattr(tv_matcher_module, "find_best_match", fake_find_best_match)
    return fake_find_best_match


@pytest.fixture
def mock_thumb_download(monkeypatch):
    """Patches episode-thumbnail downloads (writes a fake jpg instead of hitting TMDB's CDN)."""
    import metamatch.tv_tagger as tv_tagger_module
    fake_bytes = b"FAKETHUMBNAILBYTES"

    def fake_download(path, match):
        dest = os.path.splitext(path)[0] + tv_tagger_module.THUMB_SUFFIX
        with open(dest, "wb") as f:
            f.write(fake_bytes)
        return dest

    monkeypatch.setattr(tv_tagger_module, "download_thumb", fake_download)
    return fake_bytes


@pytest.fixture
def mock_tv_series_details(monkeypatch):
    """Patches TMDB series/season detail lookups and image downloads so
    series-metadata tests never touch the network."""
    import metamatch.tv_matcher as tv_matcher_module
    import metamatch.tv_tagger as tv_tagger_module

    def fake_series_details(series_id):
        return {
            "series_tmdb_id": series_id,
            "name": "Breaking Bad",
            "overview": "A chemistry teacher turns to cooking meth.",
            "first_air_date": "2008-01-20",
            "year": "2008",
            "genres": ["Drama", "Crime"],
            "vote_average": 8.9,
            "status": "Ended",
            "poster_path": "/series_poster.jpg",
            "poster_url_full": "https://image.tmdb.org/t/p/original/series_poster.jpg",
        }

    def fake_season_poster(series_id, season):
        return f"https://image.tmdb.org/t/p/original/season{season}.jpg"

    def fake_download(url, dest):
        with open(dest, "wb") as f:
            f.write(b"FAKEIMAGEBYTES:" + url.encode())
        return dest

    monkeypatch.setattr(tv_matcher_module, "fetch_series_details", fake_series_details)
    monkeypatch.setattr(tv_matcher_module, "fetch_season_poster_url", fake_season_poster)
    monkeypatch.setattr(tv_tagger_module, "download_image", fake_download)
    return fake_series_details
