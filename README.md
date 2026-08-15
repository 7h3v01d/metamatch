# MetaMatch

A local tool that scans your music folder, reads whatever tags/filenames it
has, looks each track up on MusicBrainz, and shows you a confidence-scored
match — then lets you write corrected tags and/or rename files, one at a
time or in bulk.

Runs entirely on your machine as a small local web app (Flask backend,
plain HTML/JS frontend — no build step, no external services other than
the MusicBrainz API for lookups).

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

## Setup

```bash
cd metamatch
pip install -r requirements.txt
python app.py
```

Then open **http://127.0.0.1:5050** in your browser.

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

- This release matches **music only** (mp3/wma/etc. via MusicBrainz), per
  your setup choice — movie matching (mp4/mkv against a database like TMDB)
  isn't wired up yet. The codebase is structured so a `movie_matcher.py`
  alongside `matcher.py` plus a similar scan/apply flow could be added
  later without touching the music path.
- MusicBrainz lookups need outbound internet access to
  `musicbrainz.org`, and cover art needs access to
  `coverartarchive.org`. If your machine restricts outbound network
  access, matching/art will silently return no results — scanning and
  manual tag entry still work offline.
- Undo restores tags and the filename, but not embedded art (there's no
  reliable "no art was here" marker to restore to) — keep the art
  checkbox off for a file until you're confident in the match if that
  matters to you.
- "Probable" duplicates are a heuristic (same MusicBrainz recording, or
  matching artist+title text) — always check the file list in a group
  before quarantining; live/remix/cover versions can share a title.
- Matching is best-effort: always sanity-check low-confidence matches
  (anything under ~70%) before bulk-applying. The confidence score is a
  blend of fuzzy text and duration matching, not a guarantee.
- This is a single-user local tool — state (scanned folder, match results)
  lives in memory while `app.py` is running and resets when you restart it.

## Project layout

```
metamatch/
  app.py                 Flask routes + in-memory session state
  core/
    scanner.py             Music: folder walking, tag reading, filename parsing
    matcher.py               Music: MusicBrainz search + confidence scoring
    tagger.py                   Music: tag writing, cover art embedding, renaming
    art.py                        Music: Cover Art Archive lookups (cached)
    dedup.py                        Music: duplicate detection, quarantine
    video_scanner.py       Movies: folder walking, ffprobe reads, filename parsing
    movie_matcher.py         Movies: TMDB search + confidence scoring
    movie_tagger.py             Movies: rename, .nfo write, poster download, embed
    config.py                     Movies: TMDB API key storage
  templates/index.html   Page shell (Music/Movies tabs)
  static/style.css         UI styling
  static/app.js               Frontend logic (fetch calls, rendering)
  requirements.txt
```

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
