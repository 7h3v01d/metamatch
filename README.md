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
  tags back into the file, rename it to `Artist - Title.ext`, or both.
- **Bulk apply** everything at or above a confidence threshold you set with
  a slider, or export a CSV report instead of touching any files.

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
   - Toggle **Write tags** / **Rename files** independently — e.g. leave
     rename off if you only want tags corrected in place.
4. **Export CSV report** at any point to get a spreadsheet of every file,
   its current tags, its matched tags, and the confidence score — useful
   for reviewing before you commit to bulk changes.

## Notes & limitations

- This release matches **music only** (mp3/wma/etc. via MusicBrainz), per
  your setup choice — movie matching (mp4/mkv against a database like TMDB)
  isn't wired up yet. The codebase is structured so a `movie_matcher.py`
  alongside `matcher.py` plus a similar scan/apply flow could be added
  later without touching the music path.
- MusicBrainz lookups need outbound internet access to
  `musicbrainz.org`. If your machine restricts outbound network access,
  matching will silently return no results — scanning and manual tag
  entry still work offline.
- Matching is best-effort: always sanity-check low-confidence matches
  (anything under ~70%) before bulk-applying. The confidence score is a
  blend of fuzzy text and duration matching, not a guarantee.
- This is a single-user local tool — state (scanned folder, match results)
  lives in memory while `app.py` is running and resets when you restart it.

## Project layout

```
metamatch/
  app.py              Flask routes + in-memory session state
  core/
    scanner.py         Folder walking, tag reading, filename parsing
    matcher.py          MusicBrainz search + confidence scoring
    tagger.py            Tag writing + file renaming
  templates/index.html  Page shell
  static/style.css        UI styling
  static/app.js             Frontend logic (fetch calls, rendering)
  requirements.txt
```
