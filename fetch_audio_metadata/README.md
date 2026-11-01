# Audiobook Metadata Fetcher → Audiobookshelf

A small, single-file Python script that reads the **title of each `.mp3` file**, looks the book up in
**open, key-free metadata APIs**, and writes an **Audiobookshelf-ready `metadata.json`** next to the
audio — so your local Audiobookshelf library shows real titles, authors, narrators, descriptions,
years and covers instead of `Track 01`.

Both folders are dynamic: point `--input` at any folder of audiobooks and `--output` at any folder
you want to use as — or supply to — an Audiobookshelf library.

```
┌──────────────────┐   1. parse filename     ┌─────────────────────────┐
│  --input          │  ─────────────────────► │  "As A Man Thinketh"    │
│  ./audiobooks     │                         │  author: James Allen    │
│                   │                         │  year: 1903             │
└──────────────────┘                         └───────────┬─────────────┘
                                                         │ 2. metadata already there? → skip
                                                         ▼
                                          ┌──────────────────────────────┐
                                          │ LibriVox · Open Library ·    │
                                          │ iTunes · Google Books        │
                                          └──────────────┬───────────────┘
                                                         ▼
┌──────────────────┐   4. write sidecars   ┌──────────────────────────────┐
│  --output         │ ◄──────────────────── │ metadata.json, cover.jpg,    │
│  ./abs-library    │                       │ <Author>/<Title>/<audio>     │
└──────────────────┘                       └──────────────────────────────┘
```

---

## Requirements

* **Python 3.9+** — standard library only. **No `pip install` needed.**
* **ffprobe** (from FFmpeg) — *optional*. Used to read existing embedded tags and audio duration.
  Without it the script still works, it just can't see tags already inside the files.

## Quick start

```powershell
# 1. Build an Audiobookshelf-ready library from ./audiobooks into ./abs-library
python fetch_audiobook_metadata.py --input ./audiobooks --output ./abs-library

# 2. Preview matches first, writing nothing
python fetch_audiobook_metadata.py -i ./audiobooks -o ./abs-library --dry-run -v
```

Then in Audiobookshelf: **Settings → Libraries → Add Library → Books → Folder** = the absolute path
you passed to `--output`.

## Pointing at your own folders (dynamic)

```powershell
# any folder, any drive, network shares / UNC paths too
python fetch_audiobook_metadata.py `
  --input  "D:\Media\Audiobooks" `
  --output "D:\Media\Audiobookshelf\Library"
```

The paths can also come from environment variables, which is handy for scheduled runs:

| Variable | Used as |
| --- | --- |
| `ABS_AUDIOBOOKS_DIR` | default for `--input` |
| `ABS_METADATA_DIR` | default for `--output` |

```powershell
$env:ABS_AUDIOBOOKS_DIR = "D:\Media\Audiobooks"
$env:ABS_METADATA_DIR    = "D:\Media\Audiobookshelf\Library"
python fetch_audiobook_metadata.py
```

## Output layouts

| `--layout` | What is written | When to use |
| --- | --- | --- |
| `library` *(default)* | `<-o>/<Author>/<Title>/` containing `metadata.json`, `cover.jpg` and the audio (hard-linked) | Your source folder is flat / messy and you want a clean library for Audiobookshelf |
| `mirror` | Same folder tree as the input, **sidecars only** (no audio) | You keep audio where it is but want metadata files in a separate folder |
| `in-place` | `metadata.json` + `cover.jpg` **next to the audio** | Your audiobooks are already organised one-folder-per-book |

Example result of the default layout:

```
abs-library/
├── James Allen/
│   └── As A Man Thinketh/
│       ├── As A Man Thinketh.mp3
│       ├── cover.jpg
│       └── metadata.json
├── Napoleon Hill/
│   └── Think And Grow Rich!/
│       ├── Think And Grow Rich!.mp3
│       ├── cover.jpg
│       └── metadata.json
└── _fetch-report.json
```

Audio is **hard-linked** by default, so the library costs no extra disk space and the originals stay
untouched. If hard links aren't possible (different drive, FAT/exFAT, some sandboxes) the script falls
back to symlink → copy and tells you once. Control it with `--audio-mode hardlink|copy|symlink|none`.

## What gets written

`metadata.json` uses exactly the schema Audiobookshelf parses
(`server/utils/generators/abmetadataGenerator.js`) — only the keys ABS understands, with the types it
expects; empty fields are omitted rather than written as `null`:

```json
{
  "title": "As a Man Thinketh",
  "authors": ["James Allen"],
  "narrators": ["Mark Cawley"],
  "genres": ["Philosophy", "Self-Help"],
  "publishedYear": "1902",
  "publisher": "LibriVox",
  "description": "Allen's books illustrate the use of the power of thought ...",
  "language": "English"
}
```

`cover.jpg` is downloaded from the best match that has artwork (Open Library covers, iTunes 600×600
artwork, or the Internet Archive item image for LibriVox recordings).

Audiobookshelf reads local metadata in this priority order (highest first):
`metadata.json → .opf → desc.txt/reader.txt → .nfo → embedded audio tags → folder structure`.
So a `metadata.json` sidecar wins over everything, and later edits you make in the Audiobookshelf UI
are stored back into that same file.

## How matching works

Each file name is parsed into title / author / year, e.g.:

| File name | Parsed |
| --- | --- |
| `As A Man Thinketh (1903) by James Allen [VeBX4WjhhXs].mp3` | *As A Man Thinketh* · James Allen · 1903 |
| `The Science of Being Great ⧸ How to be a Genius (1911) by Wallace D. Wattles [dJC666B9mbg].mp3` | *The Science of Being Great* · subtitle *How to be a Genius* · 1911 |
| `The Book of Aquarius ⭐ By Anonymous Author. FULL Audiobook [_KucHrbp-Ao].mp3` | *The Book of Aquarius* · author unknown |
| `Some Book.mp3` | *Some Book* |

Trailing `[youtube-id]`-style IDs, `(Unabridged)`, `FULL Audiobook`, emoji and similar noise are
stripped. Then four open sources are queried **in parallel**:

| Source | Gives | Key needed |
| --- | --- | --- |
| [LibriVox](https://librivox.org/api/info) | narrator names, duration, public-domain descriptions | no |
| [Open Library](https://openlibrary.org/developers/api) | descriptions, subjects, covers, publisher, ISBN | no |
| [iTunes Search](https://performance-partners.apple.com/search-api) | audiobook listings, descriptions, 600px artwork | no |
| [Google Books](https://developers.google.com/books) | descriptions, ISBN (heavily rate limited) | no |

Every candidate is scored 0–1 against the parsed title, author, year and (when known) duration:

```
score = 0.62 · title similarity + 0.38 · author similarity
        + year bonus + "has description" bonus + source weight − duration mismatch penalty
```

The best candidate above `--min-score` (default `0.60`) is written. If nothing clears the bar, the
script still writes a `metadata.json` built from the file name (title/author/year) and reports the
book as `fallback`, so nothing is left without metadata.

Results are cached in `<-o>/.cache/`, so re-runs and `--dry-run` are near-instant.

## "Does metadata already exist?"

A book is skipped when any of these is found (in the target folder or next to the source audio):

* a `metadata.json` with a title **and** (authors or description or narrators)
* an `.opf` or `.nfo` file
* embedded audio tags with both a title and an artist (needs `ffprobe`)

These are **not** treated as complete, and are retried:

* a `metadata.json` written by a previous unmatched run (marked internally with `_absmetaStatus`,
  a key Audiobookshelf ignores)
* a `desc.txt` / `reader.txt` on its own

Use `--force` to refetch and overwrite everything regardless.

## All options

| Flag | Default | Meaning |
| --- | --- | --- |
| `-i, --input DIR` | `$ABS_AUDIOBOOKS_DIR` or `./audiobooks` | Folder with audiobook files (scanned recursively) |
| `-o, --output DIR` | `$ABS_METADATA_DIR` or `./abs-library` | Folder for metadata / library output |
| `--layout {library,mirror,in-place}` | `library` | See [output layouts](#output-layouts) |
| `--audio-mode {hardlink,copy,symlink,none}` | `hardlink` | How audio is placed in `library` layout |
| `--keep-audio-names` | off | Keep original file names instead of `<Title>.<ext>` |
| `--sources LIST` | `librivox,openlibrary,itunes,googlebooks` | Which providers to query |
| `--min-score N` | `0.60` | Minimum 0–1 score to accept an online match |
| `--extensions LIST` | common audio + video | Extensions to scan, e.g. `.mp3,.m4b` |
| `--force` | off | Ignore existing metadata and refetch |
| `--dry-run` | off | Show what would happen, write nothing |
| `--offline` | off | No network calls; filename-derived metadata only |
| `--no-covers` | off | Skip cover downloads |
| `--no-cache` | off | Bypass the response cache |
| `--workers N` | `4` | Parallel source requests per book |
| `--only GLOB` | — | Only books whose file name matches, e.g. `--only "*Wattles*"` |
| `--limit N` | — | Only process the first N books |
| `--report PATH` | `<-o>/_fetch-report.json` | Where the JSON report goes |
| `-v, --verbose` | off | Show the top candidates and their scores |
| `-q, --quiet` | off | Only errors + summary |

Exit codes: `0` success, `1` some book failed, `2` bad input (no files / unknown source / bad path).

## Common recipes

```powershell
# See why a book matched (or didn't)
python fetch_audiobook_metadata.py -i ./audiobooks -o ./abs-library --dry-run -v

# Strict matching: refuse weak matches, keep filename-only metadata instead
python fetch_audiobook_metadata.py -i ./audiobooks -o ./abs-library --min-score 0.8

# Only the public-domain sources, nothing else
python fetch_audiobook_metadata.py -i ./audiobooks -o ./abs-library --sources librivox,openlibrary

# No extra disk space, keep audio where it is
python fetch_audiobook_metadata.py -i ./audiobooks -o ./metadata --layout mirror

# Rebuild one author's metadata after fixing file names
python fetch_audiobook_metadata.py -i ./audiobooks -o ./abs-library --force --only "*Wattles*"
```

## Troubleshooting

**"no source returned results" / `fallback` status** — the book isn't in any open catalogue (recent
self-published titles are common). The script writes title/author/year from the file name so
Audiobookshelf still has something; match it manually in the UI (**Match** button), or rename the
file to `Title (Year) by Author.mp3` and re-run.

**"matched on title only … review"** — the file name had no author (e.g. *Anonymous Author*), so the
match is based on the title alone and may be the wrong edition. Review those in Audiobookshelf.

**Google Books is skipped partway through** — it rate limits anonymous traffic with HTTP 429. The
script skips a host that keeps refusing instead of retrying for minutes. Drop it with
`--sources librivox,openlibrary,itunes` if it bothers you.

**Audio was "copied" instead of linked** — hard links need the source and output on the same NTFS
volume. Use `--layout mirror` / `--audio-mode symlink`, or point both folders at the same drive.

**Paths with spaces** — always quote them: `-i "D:\My Audiobooks"`. Relative paths are resolved from
the current working directory.

## Limitations

* Matching is heuristic: online catalogues have no idea about your specific rip, so odd file names
  and very new books can mismatch. `--dry-run -v` shows exactly which candidate won and why.
* Multi-part audiobooks should live in **one sub-folder per book**; files loose in a single flat
  folder are treated as one book each (which is right for single-file books like the examples).
* Video containers (`.webm`, `.mp4`, …) are scanned and deduplicated against an audio twin of the
  same title, but prefer keeping the `.mp3`/`.m4b`.
* No Audible/Audnex data (it is not key-free and its terms restrict scraping).
