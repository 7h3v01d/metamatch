"""
MetaMatch
A modular toolkit for matching local music/movie files against
MusicBrainz/TMDB metadata and reconciling tags, filenames, cover art,
and duplicates.

Public API:

    from metamatch import MusicLibrary, MovieLibrary

    lib = MusicLibrary()
    lib.scan("/path/to/music")
    lib.match()
    lib.apply_all(min_confidence=85)

See library.py for the full MusicLibrary / MovieLibrary API, or README.md
for the local web-app usage (app.py) built on top of this package.

The individual modules (scanner, matcher, tagger, art, dedup,
video_scanner, movie_matcher, movie_tagger, config) are also importable
directly if you only need one piece - e.g. `from metamatch.matcher import
score_candidate` - without pulling in the stateful library classes.
"""

from .library import MusicLibrary, MovieLibrary, TvLibrary

__version__ = "0.2.1"
__all__ = ["MusicLibrary", "MovieLibrary", "TvLibrary", "__version__"]
