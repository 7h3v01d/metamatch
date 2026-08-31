# MetaMatch

*Version 0.2.4 · Apache-2.0 · by Leon Priest. A local, single-user desktop
tool — no telemetry, no account, no cloud; the only network calls are the
metadata lookups you trigger (MusicBrainz / TMDB / Cover Art Archive).*

A local tool that scans your media folder, reads whatever tags/filenames it
has, looks each item up against an online database, and shows you a
confidence-scored match — then lets you write corrected metadata and/or
rename files, one at a time or in bulk. It handles three kinds of media:

- **Music** → MusicBrainz (with cover art from the Cover Art Archive)
- **Movies** → TMDB (with `.nfo` sidecars and posters)
- **TV shows** → TMDB (episode matching, `.nfo` sidecars, thumbnails, plus
  series-level `tvshow.nfo` and season posters)

It's built in two layers:

- **`metamatch/`** — a plain Python library with no web dependency. All the
  actual scan/match/apply/undo/duplicate logic lives here as `MusicLibrary`,
  `MovieLibrary`, and `TvLibrary`. Import and use it directly in a script,
  notebook, or another application — see "Using it as a library" below.
- **`app.py`** — a small local web app (Flask backend, plain HTML/JS
  frontend, no build step) that's just a thin adapter translating HTTP
  requests into calls against one `MusicLibrary`/`MovieLibrary`/`TvLibrary`
  instance each.

If you only want the app, none of that matters — `python app.py` and go.
If you want to embed the matching/tagging logic into something bigger,
the library layer is the point: see "Using it as a library".

MetaMatch edits irreplaceable media in place, so it's built to be
paranoid about it: every destructive operation re-checks filesystem
authority and content identity at the moment of mutation, is recorded in
a persistent journal *before* any file is touched, and either completes,
rolls back to its captured before-state, or is flagged for a manual check
— never left silently half-done. See "Safety model" and "Undo history and
crash recovery" below.

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

**Movies and TV** work the same way, against TMDB instead of MusicBrainz —
see "Movie matching" and "TV matching" below for what's different (episode
filename parsing, `.nfo`/poster/thumbnail sidecars, container-level tag
embedding, and series-level metadata). The same scan → match → review →
apply → undo flow, the same confidence slider and CSV export, the same
duplicate detection and quarantine, and the same journal-backed undo and
crash recovery apply to all three.

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

`TvLibrary` is the episode analogue — it parses `Show.S01E02.Title.ext`
(and `1x02`, multi-episode files, `Season NN/` subfolders), matches each
episode against TMDB, and applies `.nfo`/thumbnail sidecars plus the
Plex/Kodi rename `Show - S01E02 - Title.ext`. It also writes series-level
artifacts (`tvshow.nfo`, series poster, season posters) at the show root:

```python
from metamatch import TvLibrary
from metamatch import config

config.set_tmdb_api_key("your-tmdb-key")

lib = TvLibrary()
lib.scan("/path/to/tv")
lib.match()
lib.apply_all(do_rename=True, do_nfo=True, do_thumb=True, min_confidence=80)
lib.write_series_metadata(min_confidence=80)   # tvshow.nfo + posters per series
```

**Why this shape:** each `MusicLibrary()`/`MovieLibrary()`/`TvLibrary()`
instance owns its own scanned files, matches, and undo history — no
module-level globals, no singleton state. Create as many as you need (one
per user session, one per background job, one per test). The package is
named `metamatch`, not `core` or something equally likely to collide with a
host project's own module names, specifically so it's safe to add as a
dependency elsewhere. The individual modules (`metamatch.scanner`,
`metamatch.matcher`, `metamatch.tagger`, etc.) are also importable on their
own if you only need one piece, without pulling in the stateful library
classes at all — see "Project layout" below for what each one does.



## Using it

The app opens on the **Music** tab; **Movies** and **TV Shows** tabs at
the top work the same way (they need a free TMDB API key first — see
"Movie matching" and "TV matching"). The walkthrough below is written for
music; movies and TV follow the identical scan → find matches → review →
apply/undo rhythm, with format-appropriate checkboxes (`.nfo`/poster for
movies, `.nfo`/thumbnail/series-metadata for TV).

1. Type or paste a folder path into the box (e.g. `/Users/you/Music`), or
   click **Browse…** to navigate to it visually, then click **Scan folder**.
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

## Safety model

MetaMatch modifies files you can't easily get back, so its central design
goal is that it only ever mutates the file you actually pointed it at,
only when it can prove that file is still what it scanned, and never in a
way it can't account for afterward. Three independent invariants back that
up.

**1. Filesystem authority is checked at the moment of mutation, not just
at scan.** Scanning a folder rejects symlinks and Windows reparse
points/junctions, and won't descend into linked subdirectories — so a
`Library/linked.mp3 -> /elsewhere/victim.mp3` never enters the working
set. But scan admission alone isn't enough, because filesystem identity is
mutable: a file that was a normal library member at scan time could be
swapped for a symlink, hard-linked to an outside inode, or have its parent
directory replaced with a junction *before* the actual apply runs. So
every destructive operation — Apply, Undo, Quarantine, and TV series
metadata — re-validates its target immediately before touching it, under a
per-file mutation lock, through a single authority gate
(`pathsafe.validate_mutation_target`) that refuses:
  - a symlink or reparse point (MetaMatch never follows a link to write
    through it);
  - anything whose *resolved* path escapes the selected library root
    (real-path containment, not a string-prefix check — a `Music_Backup`
    sibling of `Music` is not "inside" it);
  - a regular file with more than one hard-link name, whose aliases can't
    be proven to all live inside the library.
Every sidecar destination (`.nfo`, poster, thumbnail) is link-checked the
same way before writing, so a planted `movie.nfo -> /outside/file` symlink
is left untouched rather than followed.

**2. Content identity is verified independently of the path.** Even when
the pathname checks out, `apply()`/`undo()`/`quarantine()` re-check the
file's size, modification time, *and* a content fingerprint against what
was recorded at scan time, and refuse if anything replaced or modified the
file since. Path authority and content identity are deliberately separate
checks — a byte-identical decoy at an authorized path passes the
fingerprint but can still fail authority (e.g. a hard link), and vice
versa. (Fingerprint details are under "Notes & limitations".)

**3. Every mutation is journalled and either completes, rolls back, or is
flagged.** Nothing is touched before the intent is written to a persistent
SQLite journal; a failure mid-apply is compensated back to the captured
before-state; a failure that *can't* be cleanly compensated becomes a
visible `RECOVERY_REQUIRED` state rather than a silent inconsistency. This
is the subject of the next section.

The one boundary this design doesn't fully close is an ultra-narrow local
race: a hostile process on the same machine could, in principle, swap a
path in the microseconds between MetaMatch validating it and the OS
actually opening it. Eliminating that entirely needs handle-relative /
no-follow OS primitives that are substantially platform-specific
(especially on Windows). For a single-user local desktop tool that's a
documented limitation, not a practical exposure — the pre-mutation
revalidation closes the realistic stale-swap gap.

## Notes & limitations

- MusicBrainz lookups need outbound internet access to
  `musicbrainz.org`, and cover art needs access to
  `coverartarchive.org`. If your machine restricts outbound network
  access, matching/art will silently return no results — scanning and
  manual tag entry still work offline.
- Undo restores tags, the filename, and (for movies/TV) a pre-existing
  `.nfo`/poster/thumbnail's exact original content, but not embedded cover
  art on music files (there's no reliable "no art was here" marker to
  restore to) — keep the art checkbox off for a file until you're
  confident in the match if that matters to you. Embedded metadata is
  cleanly reversible for `.mp4`/`.m4v`; the
  `.mkv`/`.avi`/`.mov`/`.wmv` path goes through an `ffmpeg` remux that
  can't be reversed in place, so an apply that embedded via remux and then
  failed a later step is flagged `RECOVERY_REQUIRED` (the file still
  carries the applied metadata) rather than being reported as a clean
  rollback.
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
- `apply()` and `quarantine()` both re-check a file's size, modification
  time, *and* content against what was recorded at scan time before
  touching it, and refuse (with a clear error) if something replaced or
  modified the file at that path in the meantime — e.g. another program
  writing to it, or a very stale scan. The content check exists because
  size+mtime alone can be beaten: overwrite a file with different bytes
  of the exact same length, then reset its modification time with
  `os.utime` (any process can do this, no special access needed), and a
  size/mtime-only check sees nothing wrong. The content signature is a
  SHA-256 of the whole file for anything under ~3MB, or three 1MB samples
  (start/middle/end) for anything larger, so checking a multi-gigabyte
  movie stays fast and bounded rather than hashing the whole thing on
  every apply. It's a strong deterrent, not a cryptographic guarantee —
  an adversary who knew exactly which byte ranges are sampled and crafted
  a file to match all three could still slip past it — but it closes the
  bypass that just needed `os.utime` and nothing else. Rescan to clear
  the error.
  `undo()` carries the same protection forward: it records a fingerprint
  of exactly what `apply()` produced, and refuses to touch a file whose
  fingerprint no longer matches when you later undo it. For movies, a
  changed `.nfo`/poster specifically is skipped (with a warning) rather
  than blocking the whole undo, since the video itself is usually still
  fine to revert; a changed video file blocks the whole undo, same as
  music. A transaction from before this check existed has no fingerprint
  to verify against, so it's let through rather than becoming permanently
  un-undoable — the same reasoning applies to older movie transactions
  and their sidecar paths (see below).
- **Browse…** is a small in-app folder picker, not your OS's native file
  dialog — browsers deliberately don't expose real filesystem paths from
  `<input type="file">` (even with a folder picked, JS only sees relative
  names), so there's no way to hand an absolute path to the backend
  through a native picker. The in-app browser calls a small read-only
  `/api/browse` endpoint that lists subdirectories of wherever you are
  and lets you navigate — reasonable here since this is a local
  single-user tool where the scan endpoint already accepts any
  filesystem path you name; browsing doesn't expose anything scanning
  didn't already implicitly allow. On Windows, navigating "up" from a
  drive root (e.g. `C:\`) shows a "This PC" list of every other drive
  letter present, since Windows has no single filesystem root the way
  `/` works on macOS/Linux — otherwise there'd be no way to reach `D:\`
  once you'd browsed into `C:\`.
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
- State (scanned folder, match results) lives in memory while
  `app.py`/a `MusicLibrary`/`MovieLibrary` instance is running and is
  lost on restart — you'll need to rescan a folder after restarting.
  Apply/undo history is different: it's backed by a persistent journal
  (see "Undo history and crash recovery" below), so it survives a
  restart even though the scan results themselves don't.

## Undo history and crash recovery

Every `apply()` writes to a small SQLite journal (`~/.metamatch/journal.sqlite`
by default) *before* touching any file, then drives that transaction
through an explicit state machine as the operation proceeds:

```
PENDING → APPLYING → COMMITTED
                 ↘ ROLLING_BACK → ROLLED_BACK
                                ↘ RECOVERY_REQUIRED
```

What this buys you:

- **Automatic rollback.** If an apply fails partway through — the disk
  fills, a permission is lost, `ffmpeg` is killed — MetaMatch compensates
  the partial work back to the captured before-state (restores overwritten
  tags, strips freshly-embedded art, deletes a just-created `.nfo`,
  restores an overwritten one from a snapshot) and the transaction ends
  `ROLLED_BACK`. Because rename is always the *last* step of an apply, a
  failed apply never moved the file, so rollback is purely in place.
- **`ROLLED_BACK` means the before-state was actually restored.** When a
  step genuinely can't be reversed — a metadata embed that went through an
  `ffmpeg` remux rewrites the container in place — the transaction is
  honestly marked `RECOVERY_REQUIRED` instead, never mislabelled as a
  clean rollback.
- **A crash mid-operation is detected, not silently lost.** If the process
  dies between starting and finishing an apply (a kill, power loss, a
  segfault), the next `MusicLibrary`/`MovieLibrary`/`TvLibrary`
  construction runs recovery: a transaction that never got past `PENDING`
  becomes a benign `INTERRUPTED` (nothing was written); one that died
  mid-mutation (`APPLYING`/`ROLLING_BACK`) escalates to
  `RECOVERY_REQUIRED`. A journal row too corrupt to parse (a torn write)
  is quarantined and flagged rather than crashing startup.
- **Recovery items are reviewable and resolvable, not just logged.** The
  web UI surfaces outstanding `RECOVERY_REQUIRED` items in a panel where
  you can see each file, read what happened, fix it by hand, and mark it
  resolved — after which it stops resurfacing. These persist across
  restarts until you clear them, so a file that genuinely needs attention
  doesn't scroll away.
- **Undo survives a restart, with authority intact.** Each transaction
  records the library root that authorised it, so an individual `undo()`
  works after a restart (before any rescan) while still re-running the
  full authority validation — link/reparse, containment, *and* hard-link
  checks — against that recorded root before restoring anything.
- **Bulk Undo requires an explicit library scope.** Because the journal is
  deliberately shared and persistent, it can accumulate history for many
  library roots over time. So "Undo all applied" refuses to run until a
  library has actually been scanned/selected — it never interprets "no
  current library" as "every library ever recorded." (Individual undo is
  still fine via the per-transaction authority above.) The scoping is by
  real filesystem containment, so a `Music_Backup` sibling of `Music`
  isn't swept up by name.
- **Repeated applies to the same file always chain back to the true
  original**, not to whatever the last apply changed it to, and this holds
  across restarts because it's enforced at the persistence layer.

What this is *not*: byte-level atomicity for every individual step. A
crash in the exact instant between writing a tag and renaming a file can
still leave that one file half-updated — but MetaMatch will *know* it was
mid-operation on that file and flag it on restart, which is what makes
both persistent undo and recovery detection possible. Some individual
steps (the `ffmpeg` remux path) already stage to a temp file and swap
atomically; see "Movie matching".

If you use the library classes directly, you can point multiple instances
at the same journal file to share undo history, or give each its own path
for isolation — pass a `metamatch.journal.Journal(path)` instance to any
constructor:

```python
from metamatch import MusicLibrary
from metamatch.journal import Journal

lib = MusicLibrary(journal=Journal("/custom/path/journal.sqlite"))
notices = lib.get_recovery_notices()          # anything interrupted last run
attention = lib.get_outstanding_recovery()     # RECOVERY_REQUIRED, persists until resolved
```

## Project layout

```
metamatch/                (repo root)
  metamatch/                 The library - importable on its own, no Flask dependency
    __init__.py                 Public API: MusicLibrary, MovieLibrary, TvLibrary
    library.py                    The three stateful orchestration classes + shared apply/undo/rollback
    journal.py                      Persistent write-ahead undo/crash-recovery log (SQLite) + state machine
    pathsafe.py                       Filesystem-authority gate: link/reparse/hard-link/containment checks
    fingerprint.py                    Content-hash file identity (defeats size+mtime-only staleness checks)
    scanner.py                      Music: folder walking, tag reading, filename parsing
    matcher.py                        Music: MusicBrainz search + confidence scoring
    tagger.py                           Music: tag writing, cover art embedding, renaming
    art.py                                Music: Cover Art Archive lookups (cached)
    dedup.py                                Shared: duplicate detection, quarantine (music + movies + TV)
    video_scanner.py                  Movies: folder walking, ffprobe reads, filename parsing
    movie_matcher.py                    Movies: TMDB search + confidence scoring
    movie_tagger.py                       Movies: rename, .nfo, poster, embed, shared ffmpeg remux
    episode_scanner.py                TV: episode filename parsing (SxxEyy / NxNN / season folders)
    tv_matcher.py                       TV: TMDB series + episode lookup, scoring
    tv_tagger.py                          TV: episode/series .nfo, thumbnails, posters, rename
    config.py                               TMDB API key storage
  app.py                    Thin Flask adapter over one MusicLibrary + MovieLibrary + TvLibrary
  templates/index.html      Page shell (Music / Movies / TV Shows tabs)
  static/style.css            UI styling
  static/app.js                  Frontend logic (fetch calls, rendering, recovery panel)
  pyproject.toml            Packaging metadata (pip install -e . to use as a library)
  requirements.txt          Deps for running the web app (includes Flask)
  requirements-dev.txt      Adds pytest for running the test suite
  pytest.ini
  tests/                     See "Running the tests" below
```

`app.py` only calls methods on `music_library`/`movie_library`/`tv_library`
(one instance of each) and translates the result to JSON — it holds no
scan/match/apply/undo logic itself. Anything in `metamatch/` can be
imported and used without app.py or Flask ever being involved; see "Using
it as a library" above.

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
  sidecars together into `_metamatch_duplicates`. If a `.nfo`/poster
  already existed before you applied a match, undo restores its actual
  original bytes (not just its filename) — snapshotted before the apply
  overwrote it, with a size cap (8MB) past which undo falls back to
  leaving the file alone rather than guessing at content it never saved.
  Sidecar paths after a rename-time naming collision (an unrelated file
  already sitting at the name a match would naturally produce) are
  tracked exactly, not reconstructed by guessing from the final filename
  — a guess can land on someone else's file. A transaction from before
  this exact tracking existed has no way to recover where its sidecar
  really went, so undo fails closed for it: the video's filename/tags
  still get restored, but its `.nfo`/poster (if any) are left alone with
  a warning, rather than risk guessing wrong and deleting something
  unrelated. Embedded-tag reverts are
  reliable for `.mp4`/`.m4v` (direct atom edit); for
  `.mkv`/`.avi`/`.mov`/`.wmv` the embed goes through an `ffmpeg` remux
  that isn't cheaply reversible — so if a later step of that apply fails,
  the transaction is flagged `RECOVERY_REQUIRED` (the video still carries
  the applied metadata) rather than falsely reported as rolled back.

## TV matching (TMDB)

TV shows use the same TMDB key as movies (click the **TV Shows** tab).
Episodes are a messier matching problem than movies — a file carries a
series name, a season, an episode number (sometimes several), and often an
episode title, almost none of which is in the container tags — so TV
leans hard on filename and folder structure:

- **Parses episode filenames** across the common conventions:
  `Show.Name.S01E02.Title.1080p.mkv`, `Show Name - 1x02 - Title.mkv`,
  multi-episode files (`S01E02E03` → episodes 2 and 3), and a
  `Show Name/Season 01/…` layout (it climbs past the `Season NN` folder to
  find the real series name, and can even read a bare `E07.mkv` inside a
  season folder). A file with no recognisable episode marker is left
  unmatched rather than guessed at.
- **Matches in two steps**: identify the series (TMDB `search/tv` on the
  parsed show name), then fetch the specific episode for its real title,
  air date, overview, and still image. A great series-name match to a show
  that has no such episode number is penalised rather than confidently
  accepted.
- **Applies** the Plex/Kodi convention: a rename to
  `Show Name - S01E02 - Episode Title.ext`, a Kodi/Jellyfin
  `<episodedetails>` `.nfo` sidecar, and the episode still saved as
  `<basename>-thumb.jpg`. Container-level tag embedding (`tvsh`/`tvsn`/
  `tves`/`stik` MP4 atoms, or an `ffmpeg` remux for other formats) is
  available too, off by default.
- **Series-level metadata.** A separate **Write series metadata** action
  writes the show-root artifacts a media server expects: a `<tvshow>`
  `.nfo`, a series `poster.jpg`, and `seasonNN-poster.jpg` posters — one
  journaled, rollback-protected transaction per series. "Undo all applied"
  reverts both episode applies and series metadata.
- Undo, duplicate detection, and quarantine work the same as music/movies,
  with one TV-specific care: an apply writes several MP4 atoms
  (`tvsh`/`tvsn`/`tves`/`stik`/`©ART`) beyond title/year, and undo restores
  each atom to its *exact* prior value — deleting only atoms that didn't
  exist before the apply, never blindly stripping pre-existing
  show/season/episode tags a file already had.

## Running the tests

```bash
pip install -r requirements-dev.txt
pytest
```

418 tests covering all three media sides plus the safety subsystems:
filename/tag parsing, match-scoring math, tag writing and
cover-art/poster/thumbnail embedding, renaming, undo (including the
"don't delete a sidecar that already existed" and "don't strip a
pre-existing TV atom" edge cases), duplicate detection and quarantine,
TMDB key storage, the `MusicLibrary`/`MovieLibrary`/`TvLibrary` classes
used directly (no Flask involved), the full Flask route layer, the
filesystem-authority gate, the journal/rollback state machine, an
extensive fault-injection matrix (disk-full, killed ffmpeg, DB locks,
journal corruption, process death at every boundary, journal-write
failures), and the accumulated adversarial-review regressions
(`test_review_020` through `test_review_023`), which permanently pin every
security finding from successive review rounds.

None of it touches the network — MusicBrainz, TMDB, and Cover Art
Archive/poster/thumbnail lookups are all monkeypatched with fixtures in
`tests/conftest.py` (`mock_music_match`, `mock_movie_match`, `mock_tv_match`,
`mock_cover_art`, `mock_poster_download`, `mock_thumb_download`,
`mock_tv_series_details`), so the suite runs offline, fast, and without
needing a TMDB API key.

Real media files (mp3/wma/flac/mp4/mkv) are generated once per test
session with `ffmpeg` and copied fresh into each test via the
`music_dir`/`movie_dir`/`tv_dir` fixtures, so tag-writing and `ffprobe`
reads are exercised against actual files, not mocks. Tests that need
`ffmpeg` are marked `@requires_ffmpeg` and skip cleanly (rather than fail)
if it isn't installed — run `pytest -v` to see which ones were skipped and
why.

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
  test_dedup.py                    Exact/probable duplicates, quarantine (all three)
  test_video_scanner.py    Movie filename parsing + ffprobe reading
  test_movie_matcher.py      TMDB scoring math
  test_movie_tagger.py         Rename, .nfo, poster, embedded metadata
  test_tv.py                       TV parsing, matching, apply/undo, series metadata, UI wiring
  test_config.py                     TMDB key storage
  test_journal.py                      Persistent write-ahead journal, in isolation
  test_fingerprint.py                    Content-hash file identity, in isolation
  test_library.py                          MusicLibrary/MovieLibrary/TvLibrary used directly, no Flask
  test_app_music.py                          Music Flask routes, end to end
  test_app_movies.py                           Movie Flask routes, end to end
  test_rollback.py                               Automatic rollback / failure-atomic apply
  test_fault_injection.py                          Disk-full, killed ffmpeg, DB locks, crash recovery, journal-write faults
  test_recovery_resolve.py                           Recovery-item resolution workflow
  test_hardening.py                                    Regressions for an adversarial security/robustness review
  test_review_020.py                                     Adversarial round: rollback/atoms/concurrency/remux/sidecar findings
  test_review_021.py                                       Adversarial round: mutation-time authority (symlink/hardlink/sidecar)
  test_review_022.py                                         Adversarial round: restart-Undo authority via library_root provenance
  test_review_023.py                                           Adversarial round: restart bulk-Undo scope
```

## License

Copyright 2026 Leon Priest. Licensed under the Apache License, Version 2.0
— see [LICENSE](LICENSE). You may use, modify, and redistribute this
software under those terms; it is provided "as is", without warranty of
any kind.
