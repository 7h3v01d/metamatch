# MetaMatch

A local tool that scans your music folder, reads whatever tags/filenames it
has, looks each track up on MusicBrainz, and shows you a confidence-scored
match — then lets you write corrected tags and/or rename files, one at a
time or in bulk. Movies work the same way against TMDB.

It's built in two layers:

- **`metamatch/`** — a plain Python library with no web dependency. All the
  actual scan/match/apply/undo/duplicate logic lives here as `MusicLibrary`
  and `MovieLibrary`. Import and use it directly in a script, notebook, or
  another application — see "Using it as a library" below.
- **`app.py`** — a small local web app (Flask backend, plain HTML/JS
  frontend, no build step) that's just a thin adapter translating HTTP
  requests into calls against one `MusicLibrary`/`MovieLibrary` instance.

If you only want the app, none of that matters — `python app.py` and go.
If you want to embed the matching/tagging logic into something bigger,
the library layer is the point: see "Using it as a library".

## What it does

- **Scans** a folder (optionally recursively) for `.mp3`, `.wma`, `.flac`,
  `.m4a`, `.ogg`, `.wav` files.
- **Reads existing tags** (ID3 for mp3, ASF for wma, generic tags for the
  rest) and, when tags are missing or unhelpful, **parses the filename**
  (stripping junk like `(Official Audio)`, `[HQ]`, leading track numbers,
  underscores, etc.) as a fallback signal.
- **Queries MusicBrainz** for each track and scores every candidate on:
  - title similarity (fuzzy string match)
  - artist similarity (fuzzy string match)
  - MusicBrainz's own relevance score
  - duration closeness (when both durations are known)
  
  These are blended into a single 0–100 **confidence score**.
- **Applies matches** you approve: write corrected artist/title/album/year
  tags back into the file, rename it to `Artist - Title.ext`, embed cover
  art, or any combination of the three.
- **Bulk apply** everything at or above a confidence threshold you set with
  a slider, or export a CSV report instead of touching any files.
- **Undo** any applied change, one file or all of them, restoring the
  original tags and filename.
- **Cover art**, fetched from the Cover Art Archive (MusicBrainz's
  companion image service — no API key needed) and embedded into the file
  (ID3 APIC for mp3, WM/Picture for wma, native picture blocks for
  flac/m4a/ogg). A thumbnail shows in the match column before you apply.
- **Duplicate detection**: an "exact" pass (identical files, by content
  hash) and a "probable" pass (same MusicBrainz recording, or same
  artist+title, in different rips/encodes). Flagged files are *moved*
  into a `_metamatch_duplicates` folder, never deleted, so it's easy to
  double-check or reverse.

## Setup (running the web app)

```bash
cd metamatch
pip install -r requirements.txt
python app.py
```

Then open **http://127.0.0.1:5050** in your browser.

## Using it as a library

Everything the web app does is really just `MusicLibrary`/`MovieLibrary`
method calls underneath. Install the package and use it directly:

```bash
pip install -e .          # editable install from this folder, or
pip install .             # regular install
# add [webapp] if you also want Flask: pip install ".[webapp]"
```

```python
from metamatch import MusicLibrary

lib = MusicLibrary()
lib.scan("/path/to/music")          # or recursive=False
lib.match()                         # synchronous; blocks until MusicBrainz lookups finish
                                     # (pass progress_callback=... or use match_async() for background use)

for track in lib.tracks_payload():
    match = track.get("match")
    if match and match["confidence"] >= 85:
        lib.apply(track["id"], do_tag=True, do_rename=True)
    elif match:
        print(f"Low confidence ({match['confidence']}%) for {track['filename']}, skipping")

lib.apply_all(min_confidence=90)    # or apply everything above a bar in one call
dupes = lib.find_duplicates()       # {"exact": [...], "probable": [...]}
csv_text = lib.export_csv()
```

`MovieLibrary` is the same shape, with `do_nfo`/`do_poster` instead of
`do_art`, and it needs a TMDB key first (`metamatch.config.set_tmdb_api_key(...)`
or the `TMDB_API_KEY` env var):

```python
from metamatch import MovieLibrary
from metamatch import config

config.set_tmdb_api_key("your-tmdb-key")

lib = MovieLibrary()
lib.scan("/path/to/movies")
lib.match()
lib.apply_all(do_rename=True, do_nfo=True, do_poster=True, min_confidence=85)
```

**Why this shape:** each `MusicLibrary()`/`MovieLibrary()` instance owns its
own scanned files, matches, and undo history — no module-level globals, no
singleton state. Create as many as you need (one per user session, one per
background job, one per test). The package is named `metamatch`, not
`core` or something equally likely to collide with a host project's own
module names, specifically so it's safe to add as a dependency elsewhere.
The individual modules (`metamatch.scanner`, `metamatch.matcher`,
`metamatch.tagger`, etc.) are also importable on their own if you only need
one piece, without pulling in the stateful library classes at all — see
"Project layout" below for what each one does.



## Using it

1. Paste a folder path into the box (e.g. `/Users/you/Music`) and click
   **Scan folder**.
2. Click **Find matches** — this queries MusicBrainz for every file found.
   MusicBrainz asks unauthenticated clients to stay around 1 request/second,
   so a library of a few hundred tracks will take a few minutes; progress
   is shown live.
3. Review the table: current tags on the left, best MusicBrainz match and
   confidence bar on the right.
   - Click **Apply** on a single row to tag/rename just that file.
   - Or set the **auto-apply threshold** slider and click **Apply to
     matches ≥ threshold** to process everything above that confidence in
     one go.
   - Toggle **Write tags** / **Rename files** / **Embed cover art**
     independently — e.g. leave rename off if you only want tags corrected
     in place.
   - Made a mistake, or the match was wrong? Click **Undo** on that row
     (or **Undo all applied** to revert everything in one go). This
     restores the original tags and filename — it does not remove
     embedded art, since art doesn't carry an easy "was this here before"
     marker; if that matters, leave the art checkbox off until you're sure.
4. **Export CSV report** at any point to get a spreadsheet of every file,
   its current tags, its matched tags, and the confidence score — useful
   for reviewing before you commit to bulk changes.
5. **Duplicates panel**: click **Scan for duplicates** to find identical
   files and probable repeat rips. Each group lists its files with size,
   duration, and match confidence; the first file in each group is left
   unchecked (kept) and the rest are checked by default. Adjust the
   checkboxes and click **Move checked files to _metamatch_duplicates** —
   files are moved into that subfolder, not deleted.

## Notes & limitations

- MusicBrainz lookups need outbound internet access to
  `musicbrainz.org`, and cover art needs access to
  `coverartarchive.org`. If your machine restricts outbound network
  access, matching/art will silently return no results — scanning and
  manual tag entry still work offline.
- Undo restores tags, the filename, and (for movies) a pre-existing
  `.nfo`/poster's exact original content, but not embedded cover art on
  music files (there's no reliable "no art was here" marker to restore
  to) — keep the art checkbox off for a file until you're confident in
  the match if that matters to you. Embedded movie metadata is only
  reversible for `.mp4`/`.m4v`; the `.mkv`/`.avi`/`.mov`/`.wmv` path goes
  through an `ffmpeg` remux that isn't cheaply undoable.
- "Probable" duplicates are a heuristic (same MusicBrainz recording/TMDB
  movie, or matching title text) — nothing is preselected for
  quarantine in that case, unlike byte-identical "exact" duplicates.
  Always check the file list in a group before quarantining;
  live/remix/cover versions can share a title.
- Matching is best-effort: always sanity-check low-confidence matches
  (anything under ~70%) before bulk-applying. The confidence score is a
  blend of fuzzy text, duration/year, and (for movies) TMDB search
  relevance ordering — it does not factor in a movie's popularity/rating,
  since that's evidence of nothing about *which* movie a file actually is.
- `apply()` re-checks a file's size and modification time against what
  was recorded at scan time before touching it, and refuses (with a
  clear error) if something replaced or modified the file at that path
  in the meantime — e.g. another program writing to it, or a very stale
  scan. Rescan to clear the error.
- Quarantine only ever accepts files that the current scan itself
  discovered — not arbitrary paths — and the `_metamatch_duplicates`
  folder it creates is excluded from future scans of the same directory.
- This is a single-user local tool bound to `127.0.0.1` with no auth
  token; the local API rejects state-changing requests whose `Origin`
  header doesn't match its own host (closing off drive-by browser CSRF),
  but it isn't hardened against other processes already running on the
  same machine.
- CSV exports neutralize values that would be interpreted as spreadsheet
  formulas (`=`, `+`, `-`, `@` prefixes) since matched metadata and
  filenames are untrusted external text by the time they reach a CSV cell.
- State (scanned folder, match results, undo history) lives in memory
  while `app.py`/a `MusicLibrary`/`MovieLibrary` instance is running and
  is lost on restart — there's no persistent transaction log, so a crash
  mid-operation can't be recovered from automatically (the underlying
  file operations are individually safe - fail-closed remux, fingerprint
  checks, collision-safe renames - but there's no cross-operation undo
  journal spanning a restart).

## Project layout

```
metamatch/                (repo root)
  metamatch/                 The library - importable on its own, no Flask dependency
    __init__.py                 Public API: MusicLibrary, MovieLibrary
    library.py                    MusicLibrary/MovieLibrary - the stateful orchestration layer
    scanner.py                      Music: folder walking, tag reading, filename parsing
    matcher.py                        Music: MusicBrainz search + confidence scoring
    tagger.py                           Music: tag writing, cover art embedding, renaming
    art.py                                Music: Cover Art Archive lookups (cached)
    dedup.py                                Shared: duplicate detection, quarantine (music + movies)
    video_scanner.py                  Movies: folder walking, ffprobe reads, filename parsing
    movie_matcher.py                    Movies: TMDB search + confidence scoring
    movie_tagger.py                       Movies: rename, .nfo write, poster download, embed
    config.py                               TMDB API key storage
  app.py                    Thin Flask adapter over one MusicLibrary + one MovieLibrary
  templates/index.html      Page shell (Music/Movies tabs)
  static/style.css            UI styling
  static/app.js                  Frontend logic (fetch calls, rendering)
  pyproject.toml            Packaging metadata (pip install -e . to use as a library)
  requirements.txt          Deps for running the web app (includes Flask)
  requirements-dev.txt      Adds pytest for running the test suite
  pytest.ini
  tests/                     See "Running the tests" below
```

`app.py` only calls methods on `music_library`/`movie_library` (one
`MusicLibrary`/`MovieLibrary` instance each) and translates the result to
JSON — it holds no scan/match/apply/undo logic itself. Anything in
`metamatch/` can be imported and used without app.py or Flask ever being
involved; see "Using it as a library" above.

## Movie matching (TMDB)

Movie matching against [TMDB](https://www.themoviedb.org) is wired up
alongside the music flow — click the **Movies** tab at the top of the
page. It works a bit differently from music, because movies and how
they're organized are different:

- **Needs a free API key.** Grab one at
  [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api)
  and paste it into the settings panel that appears the first time you
  open the Movies tab. It's saved locally to `~/.metamatch/config.json`
  (plaintext — this is a local single-user tool, so that's an acceptable
  tradeoff, but don't share that file). You can also set it via the
  `TMDB_API_KEY` environment variable instead, which takes priority.
- **Scans** `.mp4`, `.mkv`, `.avi`, `.mov`, `.wmv`, `.m4v` files and uses
  `ffprobe` (part of ffmpeg) to read duration and any embedded title/date
  tags, uniformly across formats. If `ffmpeg`/`ffprobe` isn't installed,
  scanning still works — you just lose duration and embedded-tag reading;
  filename parsing (see below) still does the heavy lifting either way.
- **Parses filenames** aggressively, since most movie files (ripped or
  downloaded) don't carry useful embedded metadata: it strips resolution
  (`1080p`, `2160p`), source/codec tags (`BluRay`, `WEBRip`, `x264`,
  `HEVC`), release-group suffixes, and pulls out a title + year — e.g.
  `The.Matrix.1999.1080p.BluRay.x264-GROUP.mkv` → title "The Matrix",
  year 1999. This is a heuristic and can occasionally misfire on titles
  that contain a number that looks like a year (`Blade Runner 2049` is
  the classic case) — check the "Parsed" column before matching.
- **Applies matches** using the convention media servers like Plex/Kodi/
  Jellyfin actually expect: a renamed file (`Title (Year).ext`), a
  Kodi-style `.nfo` XML sidecar with title/year/plot/rating, and a saved
  poster image (`Title (Year)-poster.jpg`) next to the video. These are
  fast — no video processing involved.
- **Embedding metadata into the container itself** is available too (the
  "Embed metadata" checkbox, off by default) but works differently by
  format: `.mp4`/`.m4v` can be edited directly and cheaply, while
  `.mkv`/`.avi`/`.mov`/`.wmv` require `ffmpeg` to remux the file with
  `-c copy` (a fast, lossless stream copy — no re-encoding — but it does
  rewrite the whole file, which matters for multi-gigabyte movies). Leave
  it off for a quick pass over a large library; turn it on for files you
  specifically want tagged at the container level.
- Undo and duplicate detection are wired up for movies too, working the
  same way as music: rename/tag reverts, an **Undo all applied** button,
  and a **Duplicates** panel (exact file hash + probable same-TMDB-movie
  grouping) that quarantines flagged videos *and* their `.nfo`/poster
  sidecars together into `_metamatch_duplicates`. One real limitation:
  undo can only remove a `.nfo`/poster it created itself — if one already
  existed before you applied a match, undo restores its filename but not
  its original content (there's no snapshot of what was there before).
  Embedded-tag reverts are reliable for `.mp4`/`.m4v` (direct atom edit);
  for `.mkv`/`.avi`/`.mov`/`.wmv` the embed goes through an `ffmpeg`
  remux that isn't cheaply reversible, so those tags are left as-is on
  undo - the same tradeoff as music's cover-art embedding.

## Running the tests

```bash
pip install -r requirements-dev.txt
pytest
```

224 tests covering both the music and movie sides: filename/tag parsing,
match-scoring math, tag writing and cover-art/poster embedding, renaming,
undo (including the "don't delete a sidecar that already existed"
edge case), duplicate detection and quarantine, TMDB key storage, the
`MusicLibrary`/`MovieLibrary` classes used directly (no Flask involved),
and the full Flask route layer for both `/api/*` and `/api/movies/*`.

None of it touches the network — MusicBrainz, TMDB, and Cover Art
Archive/poster lookups are all monkeypatched with fixtures in
`tests/conftest.py` (`mock_music_match`, `mock_movie_match`,
`mock_cover_art`, `mock_poster_download`), so the suite runs offline,
fast (~4s), and without needing a TMDB API key.

Real media files (mp3/wma/flac/mp4/mkv) are generated once per test
session with `ffmpeg` and copied fresh into each test via the
`music_dir`/`movie_dir` fixtures, so tag-writing and `ffprobe` reads are
exercised against actual files, not mocks. Tests that need `ffmpeg` are
marked `@requires_ffmpeg` and skip cleanly (rather than fail) if it isn't
installed — run `pytest -v` to see which ones were skipped and why.

Config-touching tests use the `isolated_config` fixture, which redirects
`metamatch/config.py` to a temp directory for the duration of the test, so
the suite never reads or writes your real `~/.metamatch/config.json`.

```
tests/
  conftest.py           Fixtures: media generation, mocks, fresh library instances
  test_scanner.py         Music filename parsing + tag reading
  test_matcher.py           MusicBrainz scoring math
  test_tagger.py               Tag writing, cover art, rename, undo helpers
  test_art.py                    Cover Art Archive fetch + cache
  test_dedup.py                    Exact/probable duplicates, quarantine (both)
  test_video_scanner.py    Movie filename parsing + ffprobe reading
  test_movie_matcher.py      TMDB scoring math
  test_movie_tagger.py         Rename, .nfo, poster, embedded metadata
  test_config.py                 TMDB key storage
  test_library.py                  MusicLibrary/MovieLibrary used directly, no Flask
  test_app_music.py                  Music Flask routes, end to end
  test_app_movies.py                   Movie Flask routes, end to end
  test_hardening.py                      Regressions for an adversarial security/robustness review
```
