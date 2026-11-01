#!/usr/bin/env python3
"""
fetch_audiobook_metadata.py
===========================

Build Audiobookshelf-ready metadata for a folder of audiobook files, using only
open, key-free metadata sources.

What it does
------------
1. Scans a *dynamic* input folder (recursively) for audio files.
2. Derives the book title / author / year from each file name
   (e.g. "As A Man Thinketh (1903) by James Allen [VeBX4WjhhXs].mp3").
3. Checks whether metadata already exists (metadata.json / .opf / .nfo /
   desc.txt / embedded ID3-style tags). Existing books are skipped.
4. For books without metadata, searches open APIs:
      - LibriVox    (public-domain audiobooks, gives narrator + duration)
      - Open Library (works, descriptions, covers, subjects)
      - iTunes      (audiobook listings, descriptions, artwork)
      - Google Books (books, descriptions, ISBN) - last resort
   Candidates are scored against the parsed title/author/year/duration.
5. Writes a sidecar `metadata.json` in the exact schema Audiobookshelf parses
   (see server/utils/generators/abmetadataGenerator.js), plus an optional
   `cover.jpg`, so Audiobookshelf picks the metadata up on library scan.
6. Optionally mirrors/copies the audio into an Audiobookshelf-ready
   `<Author>/<Title>/` folder structure (`--layout library`, the default).

Outputs are written to a *dynamic* output folder (`--output`).

No third-party Python packages are required (stdlib only).

Examples
--------
  # Scan ./audiobooks, build an ABS-ready library in ./abs-library
  python fetch_audiobook_metadata.py --input ./audiobooks --output ./abs-library

  # Only write sidecar metadata, keep audio where it is
  python fetch_audiobook_metadata.py -i ./audiobooks -o ./metadata --layout in-place

  # Preview what would be matched, write nothing
  python fetch_audiobook_metadata.py -i ./audiobooks -o ./abs-library --dry-run -v

  # Re-fetch everything, overwriting existing metadata
  python fetch_audiobook_metadata.py -i ./audiobooks -o ./abs-library --force

Metadata priority note
----------------------
Audiobookshelf reads local metadata in this order (default): metadata.json >
.opf > desc.txt/reader.txt > .nfo > embedded file tags > folder structure.
So a valid metadata.json sidecar in the book folder wins over everything else.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

__version__ = "1.0.0"

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

USER_AGENT = f"abs-metadata-fetcher/{__version__} (+https://github.com/advplyr/audiobookshelf)"

#: Extensions Audiobookshelf can play / that we treat as audiobook audio.
AUDIO_EXTENSIONS = {
    ".mp3", ".m4b", ".m4a", ".m4c", ".flac", ".ogg", ".oga", ".opus",
    ".aac", ".wav", ".wma", ".aif", ".aiff", ".mka",
}
#: Video containers that often carry an audiobook rip. Linked only if no audio twin.
VIDEO_EXTENSIONS = {".webm", ".mp4", ".m4v", ".mkv", ".mov", ".avi", ".flv"}

DEFAULT_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS

#: Values treated as "no author known".
ANONYMOUS_AUTHORS = {
    "anonymous", "anonymous author", "anon", "unknown", "unknown author",
    "various", "various authors", "var", "n a", "na", "none", "author",
    "public domain",
}

LANGUAGE_NAMES = {
    "eng": "English", "en": "English", "en-us": "English", "en-gb": "English",
    "fre": "French", "fra": "French", "fr": "French",
    "spa": "Spanish", "es": "Spanish", "ger": "German", "deu": "German", "de": "German",
    "ita": "Italian", "it": "Italian", "por": "Portuguese", "pt": "Portuguese",
    "rus": "Russian", "ru": "Russian", "dut": "Dutch", "nld": "Dutch", "nl": "Dutch",
    "pol": "Polish", "pl": "Polish", "swe": "Swedish", "sv": "Swedish",
    "jpn": "Japanese", "ja": "Japanese", "chi": "Chinese", "zho": "Chinese",
    "lat": "Latin", "grc": "Greek", "gre": "Greek", "el": "Greek",
    "hin": "Hindi", "ara": "Arabic", "heb": "Hebrew", "tur": "Turkish",
    "dan": "Danish", "nor": "Norwegian", "fin": "Finnish", "cze": "Czech",
}

SOURCE_WEIGHTS = {
    # Slight preference for audiobook-native catalogues.
    "librivox": 0.02,
    "itunes": 0.01,
    "openlibrary": 0.0,
    "googlebooks": -0.01,
}


# --------------------------------------------------------------------------- #
# Small helpers: logging, text normalisation
# --------------------------------------------------------------------------- #

class Log:
    quiet = False
    verbose = False

    @classmethod
    def info(cls, msg: str) -> None:
        if not cls.quiet:
            print(msg, flush=True)

    @classmethod
    def detail(cls, msg: str) -> None:
        if cls.verbose and not cls.quiet:
            print(msg, flush=True)

    @classmethod
    def warn(cls, msg: str) -> None:
        print(f"  ! {msg}", file=sys.stderr, flush=True)

    @classmethod
    def error(cls, msg: str) -> None:
        print(f"  x {msg}", file=sys.stderr, flush=True)


def norm_text(value: str | None) -> str:
    """Lowercase, strip accents/punctuation, collapse whitespace."""
    if not value:
        return ""
    s = unicodedata.normalize("NFKD", value)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def strip_symbols(value: str) -> str:
    """Drop emoji/symbol characters that are useless in titles or paths."""
    keep = []
    for ch in value:
        cat = unicodedata.category(ch)
        if cat in ("So", "Sk", "Cs", "Cf", "Co"):
            continue
        keep.append(ch)
    return "".join(keep)


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def collapse_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def strip_html(value: str) -> str:
    """Turn API HTML descriptions into plain text for Audiobookshelf."""
    if not value:
        return ""
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", value)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|li|h[1-6])>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = s.replace("\r\n", "\n").replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def safe_name(value: str, maxlen: int = 110, fallback: str = "Unknown") -> str:
    """Make a string safe to use as a Windows/macOS/Linux file or folder name."""
    s = strip_symbols(value or "")
    s = re.sub(r'[<>:"/\\|?*]', " ", s)
    s = re.sub(r"[\x00-\x1f]", "", s)
    s = collapse_ws(s).strip(" .")
    if not s:
        s = fallback
    if len(s) > maxlen:
        s = s[:maxlen].rstrip(" .")
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
    if s.upper() in reserved:
        s = f"_{s}"
    return s


def truncate(value: str, limit: int = 120) -> str:
    value = collapse_ws(value)
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------- #
# File-name parsing
# --------------------------------------------------------------------------- #

@dataclass
class ParsedName:
    """What we can learn about a book from its file name alone."""
    title: str
    author: str | None = None
    subtitle: str | None = None
    year: int | None = None
    edition: str | None = None
    video_id: str | None = None
    raw_stem: str = ""
    extras: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "author": self.author,
            "subtitle": self.subtitle,
            "year": self.year,
            "videoId": self.video_id,
        }


_NOISE_WORDS = r"(?:full\s+)?(?:audio\s*book|audiobook|unabridged|abridged|complete\s+edition)"
_BRACKET_NOISE_RE = re.compile(rf"[\[\(]\s*{_NOISE_WORDS}\s*[\]\)]", re.I)
_TRAILING_NOISE_RE = re.compile(
    rf"[\s\-–—,.|:/]*(?:\b{_NOISE_WORDS}\b[\s\-–—,.|:/]*)+$", re.I
)
_VIDEO_ID_RE = re.compile(r"[\s\-–—]*[\[\(](?P<vid>[A-Za-z0-9_-]{11})[\]\)]\s*$")
_YEAR_RE = re.compile(r"[\(\[]\s*(?P<year>(?:1[5-9]\d{2}|20\d{2}))\b(?P<rest>[^\)\]]*)[\)\]]")
_BY_RE = re.compile(r"(?:^|\s)(?:by|By|BY)\s+")
_SUBTITLE_SEP_RE = re.compile(r"\s+(?:⧸|｜|│|／|/|\|)\s+")


def _clean_title(raw: str) -> str:
    s = strip_symbols(raw)
    s = _BRACKET_NOISE_RE.sub(" ", s)
    s = _TRAILING_NOISE_RE.sub(" ", s)
    s = s.replace("—", " - ").replace("–", " - ")
    s = collapse_ws(s)
    s = s.strip(" .-–—|,;:")
    return s


def _clean_author(raw: str) -> str | None:
    if not raw:
        return None
    s = strip_symbols(raw)
    s = re.sub(rf"[\s\-–—,.|:/]*\b{_NOISE_WORDS}\b.*$", "", s, flags=re.I)
    s = re.sub(r"\s*[\(\[]\s*[^\)\]]*[\)\]]\s*$", "", s)  # trailing "(narrator)" etc.
    s = s.strip(" .-–—,;:|/")
    s = collapse_ws(s)
    if not s or norm_text(s) in ANONYMOUS_AUTHORS:
        return None
    if len(s) > 70:
        return None
    # "JAMES ALLEN" / "james allen" -> "James Allen"
    if s.isupper() or s.islower():
        s = " ".join(part.capitalize() if part.isupper() or part.islower() else part for part in s.split())
    return s


def parse_audio_filename(stem: str) -> ParsedName:
    """Parse "Title (Year) by Author [videoId]" style names."""
    raw_stem = stem
    s = stem.replace("_", " ").strip()

    video_id = None
    m = _VIDEO_ID_RE.search(s)
    if m:
        video_id = m.group("vid")
        s = s[: m.start()].strip()

    # Split off the author using the *last* " by " occurrence.
    title_part, author_part = s, ""
    by_matches = list(_BY_RE.finditer(s))
    if by_matches:
        last = by_matches[-1]
        candidate_author = s[last.end():].strip()
        if candidate_author and not re.fullmatch(r"[\d\W]+", candidate_author):
            title_part = s[: last.start()].strip(" -–—,|:")
            author_part = candidate_author

    year = None
    edition = None
    m = _YEAR_RE.search(title_part)
    if m:
        year = int(m.group("year"))
        edition = m.group("rest").strip(" -–—,()[]") or None
        title_part = (title_part[: m.start()] + " " + title_part[m.end():]).strip()

    subtitle = None
    parts = _SUBTITLE_SEP_RE.split(title_part, maxsplit=1)
    if len(parts) == 2 and all(p.strip() for p in parts):
        title_part, subtitle = parts[0], parts[1]

    title = _clean_title(title_part)
    subtitle = _clean_title(subtitle) if subtitle else None
    if not title:
        title = _clean_title(s) or raw_stem

    return ParsedName(
        title=title,
        author=_clean_author(author_part),
        subtitle=subtitle or None,
        year=year,
        edition=edition,
        video_id=video_id,
        raw_stem=raw_stem,
    )


# --------------------------------------------------------------------------- #
# Candidate metadata
# --------------------------------------------------------------------------- #

@dataclass
class Candidate:
    """A normalised search result from any source."""
    source: str
    title: str
    authors: list[str] = field(default_factory=list)
    narrators: list[str] = field(default_factory=list)
    series: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    subtitle: str | None = None
    published_year: int | None = None
    published_date: str | None = None
    publisher: str | None = None
    description: str = ""
    isbn: str | None = None
    asin: str | None = None
    language: str | None = None
    cover_url: str | None = None
    source_url: str | None = None
    duration_secs: int | None = None
    abridged: bool | None = None
    explicit: bool | None = None
    score: float = 0.0

    def identifier(self) -> str:
        return f"{self.source}:{self.title}:{(self.authors or [''])[0]}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "title": self.title,
            "subtitle": self.subtitle,
            "authors": self.authors,
            "narrators": self.narrators,
            "genres": self.genres,
            "publishedYear": self.published_year,
            "publisher": self.publisher,
            "language": self.language,
            "isbn": self.isbn,
            "asin": self.asin,
            "hasDescription": bool(self.description),
            "coverUrl": self.cover_url,
            "sourceUrl": self.source_url,
            "score": round(self.score, 3),
        }


def title_similarity(parsed: ParsedName, cand: Candidate) -> float:
    qt = norm_text(parsed.title)
    if not qt:
        return 0.0
    variants = [cand.title]
    if cand.subtitle:
        variants.append(f"{cand.title} {cand.subtitle}")
    best = 0.0
    q_tokens = set(qt.split())
    for raw in variants:
        ct = norm_text(raw)
        if not ct:
            continue
        ratio = similarity(qt, ct)
        if ct == qt or ct.startswith(qt + " ") or ct.startswith(qt):
            ratio = max(ratio, 0.96)
        else:
            c_tokens = set(ct.split())
            if q_tokens and q_tokens <= c_tokens:
                # All query words appear in the candidate title (extra subtitle words).
                ratio = max(ratio, 0.90)
        best = max(best, ratio)
    return best


def author_similarity(author: str | None, candidates: Sequence[str]) -> float:
    qa = norm_text(author)
    if not qa:
        return 0.0
    best = 0.0
    q_last = qa.split()[-1] if qa.split() else ""
    for name in candidates:
        ca = norm_text(name)
        if not ca:
            continue
        ratio = similarity(qa, ca)
        if qa in ca or ca in qa:
            ratio = max(ratio, 0.90)
        if q_last and q_last in ca.split():
            ratio = max(ratio, 0.85)
        best = max(best, ratio)
    return best


def select_authors(parsed_author: str | None, authors: Sequence[str]) -> list[str]:
    """
    Trim contributor lists to the people the file name points at.

    Work records (especially Open Library) often list translators, editors or
    narrators alongside the author, e.g. ["Wallace D. Wattles", "Lou Harshaw"].
    When the file name names an author, keep only plausible matches.
    """
    cleaned = [collapse_ws(a) for a in authors if isinstance(a, str) and 0 < len(a) <= 70]
    if not cleaned:
        return []
    if not parsed_author:
        return cleaned[:3]

    keep = [a for a in cleaned if author_similarity(parsed_author, [a]) >= 0.60]
    if not keep:
        keep = [parsed_author]
    unique: list[str] = []
    for name in keep:
        if name not in unique:
            unique.append(name)
    return unique[:3]


def score_candidate(parsed: ParsedName, cand: Candidate, duration: float | None = None) -> float:
    t_sim = title_similarity(parsed, cand)
    if parsed.author:
        a_sim = author_similarity(parsed.author, cand.authors)
        score = 0.62 * t_sim + 0.38 * a_sim
    else:
        score = 0.95 * t_sim

    if parsed.year and cand.published_year and abs(parsed.year - cand.published_year) <= 2:
        score += 0.04
    if cand.description:
        score += 0.02
    score += SOURCE_WEIGHTS.get(cand.source, 0.0)

    if duration and cand.duration_secs:
        delta = abs(duration - cand.duration_secs) / max(duration, cand.duration_secs, 1)
        if delta > 0.6:
            score -= 0.10
        elif delta < 0.15:
            score += 0.03

    return max(0.0, min(1.0, score))


# --------------------------------------------------------------------------- #
# HTTP client with caching + retries
# --------------------------------------------------------------------------- #

class HttpClient:
    def __init__(self, cache_dir: Path | None, timeout: float = 25.0):
        self.cache_dir = cache_dir
        self.timeout = timeout
        self._lock = threading.Lock()
        self.requests = 0
        self.cache_hits = 0
        self.failures: list[str] = []
        #: Hosts that returned 429/5xx repeatedly; skipped for the rest of the run.
        self._throttled: dict[str, int] = {}
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)

    # -- cache ------------------------------------------------------------- #
    def _cache_path(self, url: str) -> Path | None:
        if not self.cache_dir:
            return None
        key = hashlib.sha1(url.encode("utf-8")).hexdigest()
        return self.cache_dir / key[:2] / f"{key}.json"

    def _cache_read(self, url: str) -> Any | None:
        path = self._cache_path(url)
        if not path or not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _cache_write(self, url: str, data: Any) -> None:
        path = self._cache_path(url)
        if not path:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass

    # -- requests ---------------------------------------------------------- #
    def _open(self, url: str) -> bytes:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, image/*;q=0.8, */*;q=0.5",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.read()

    def get_json(self, url: str, use_cache: bool = True) -> Any | None:
        if use_cache:
            cached = self._cache_read(url)
            if cached is not None:
                self.cache_hits += 1
                return cached

        host = urllib.parse.urlsplit(url).netloc
        if self._throttled.get(host, 0) >= 2:
            # Give up on a host that keeps throttling us (e.g. Google Books 429s).
            return None

        last_error = ""
        for attempt in range(3):
            try:
                with self._lock:
                    self.requests += 1
                raw = self._open(url)
                data = json.loads(raw.decode("utf-8", "replace"))
                if use_cache:
                    self._cache_write(url, data)
                return data
            except urllib.error.HTTPError as exc:
                last_error = f"HTTP {exc.code}"
                if exc.code == 404:
                    return None
                if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                    if exc.code == 429:
                        with self._lock:
                            self._throttled[host] = self._throttled.get(host, 0) + 1
                        if self._throttled[host] >= 2:
                            Log.detail(f"    {host} is rate limiting; skipping it for this run")
                            break
                    time.sleep(2.0 * (attempt + 1))
                    continue
                break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = str(exc)
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break
            except json.JSONDecodeError as exc:
                last_error = f"invalid JSON: {exc}"
                break
        self.failures.append(f"{url.split('?')[0]} -> {last_error}")
        return None

    def get_bytes(self, url: str) -> bytes | None:
        try:
            with self._lock:
                self.requests += 1
            return self._open(url)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            Log.detail(f"    cover download failed: {exc}")
            return None


# --------------------------------------------------------------------------- #
# Metadata sources (all open, no API key)
# --------------------------------------------------------------------------- #

def _first_cover_from_archive(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(r"archive\.org/(?:details|download|compress)/([^/?#]+)", url)
    if m:
        return f"https://archive.org/services/img/{m.group(1)}"
    return None


def _clean_genres(values: Iterable[Any], limit: int = 5) -> list[str]:
    """
    Normalise genre/subject data.

    Open Library subjects are crowd-sourced and noisy: localised strings
    ("Filosofía", "Наука (общее)//Научно-популярное"), language-material notes
    and BISAC concatenations. Keep only short, Latin-script, meaningful entries.
    """
    language_words = {v.lower() for v in LANGUAGE_NAMES.values()}
    stop = {
        "general", "unknown", "n/a", "literature", "translations",
        "english language", "american literature", "british literature",
    }
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, dict):
            value = value.get("name") or value.get("title") or ""
        if not isinstance(value, str):
            continue
        g = collapse_ws(value.replace("--", " - "))
        if "//" in g:
            g = g.split("//")[0]
        g = g.strip(" .,;:-/")
        low = g.lower()
        if not g or len(g) > 50 or low in stop:
            continue
        if "language materials" in low or any(word in low for word in ("translations", "juvenile fiction")):
            continue
        if low in language_words:
            continue
        if any(ord(ch) > 127 for ch in g):  # localised subject noise
            continue
        if re.search(r"\d{3,}", g):
            continue
        if g.islower():
            g = g.title()
        if low in seen:
            continue
        seen.add(low)
        out.append(g)
        if len(out) >= limit:
            break
    return out


def search_librivox(client: HttpClient, parsed: ParsedName, duration: float | None) -> list[Candidate]:
    """LibriVox: public-domain *audiobooks*, so narrator + duration come for free."""
    query = urllib.parse.quote_plus(parsed.title)
    url = (
        "https://librivox.org/api/feed/audiobooks/"
        f"?format=json&limit=10&extended=1&title={query}"
    )
    data = client.get_json(url)
    books = (data or {}).get("books") or []
    results: list[Candidate] = []
    for book in books:
        authors: list[str] = []
        for a in book.get("authors") or []:
            name = " ".join(x for x in (a.get("first_name"), a.get("last_name")) if x).strip()
            if name and name not in authors:
                authors.append(name)

        narrators: list[str] = []
        for section in book.get("sections") or []:
            for reader in section.get("readers") or []:
                name = (reader.get("display_name") or "").strip()
                if name and name not in narrators and len(narrators) < 8:
                    narrators.append(name)

        year = book.get("copyright_year")
        try:
            year = int(str(year)[:4]) if year else None
        except (TypeError, ValueError):
            year = None

        language = book.get("language")
        if isinstance(language, str) and language:
            language = LANGUAGE_NAMES.get(language.lower(), language)

        results.append(
            Candidate(
                source="librivox",
                title=collapse_ws(book.get("title") or ""),
                authors=authors,
                narrators=narrators,
                genres=_clean_genres(book.get("genres") or []),
                published_year=year,
                publisher="LibriVox",
                description=strip_html(book.get("description") or ""),
                language=language or None,
                cover_url=_first_cover_from_archive(book.get("url_iarchive") or book.get("url_zip_file")),
                source_url=book.get("url_librivox"),
                duration_secs=int(book["totaltimesecs"]) if str(book.get("totaltimesecs") or "").isdigit() else None,
            )
        )
    return results


def _openlibrary_description(client: HttpClient, key: str) -> str:
    data = client.get_json(f"https://openlibrary.org{key}.json")
    if not isinstance(data, dict):
        return ""
    desc = data.get("description")
    if isinstance(desc, dict):
        desc = desc.get("value")
    return strip_html(desc or "")


def search_openlibrary(client: HttpClient, parsed: ParsedName, duration: float | None) -> list[Candidate]:
    """Open Library: rich work data, descriptions, covers, subjects."""
    query = parsed.title + (f" {parsed.author}" if parsed.author else "")
    fields = "key,title,subtitle,author_name,first_publish_year,publisher,subject,language,isbn,cover_i"
    url = (
        "https://openlibrary.org/search.json?limit=10"
        f"&fields={fields}&q={urllib.parse.quote_plus(query)}"
    )
    data = client.get_json(url)
    docs = (data or {}).get("docs") or []

    results: list[Candidate] = []
    for doc in docs[:6]:
        key = doc.get("key") or ""
        isbns = [str(i) for i in (doc.get("isbn") or [])]
        isbn = next((i for i in isbns if len(i) == 13), isbns[0] if isbns else None)

        languages = [LANGUAGE_NAMES.get(str(l).lower(), str(l)) for l in (doc.get("language") or [])]
        language = "English" if "English" in languages else (languages[0] if languages else None)

        cover_id = doc.get("cover_i")
        title = collapse_ws(doc.get("title") or "")
        subtitle = collapse_ws(doc.get("subtitle") or "") or None

        # Only spend a request on descriptions for plausible matches.
        plausible = title_similarity(
            parsed, Candidate(source="openlibrary", title=title, subtitle=subtitle)
        ) >= 0.45
        results.append(
            Candidate(
                source="openlibrary",
                title=title,
                subtitle=subtitle,
                authors=[collapse_ws(a) for a in (doc.get("author_name") or [])][:5],
                genres=_clean_genres(doc.get("subject") or []),
                published_year=doc.get("first_publish_year"),
                publisher=(doc.get("publisher") or [None])[0],
                description=_openlibrary_description(client, key) if key and plausible else "",
                isbn=isbn,
                language=language,
                cover_url=f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg" if cover_id else None,
                source_url=f"https://openlibrary.org{key}" if key else None,
            )
        )
    return results


def _itunes_clean_name(name: str) -> tuple[str, str | None, bool | None]:
    """Split an iTunes collection name into (title, subtitle, abridged)."""
    abridged = None
    if re.search(r"\(abridged\)", name, re.I):
        abridged = True
    elif re.search(r"\((un)?abridged\)", name, re.I):
        abridged = False
    name = re.sub(r"\s*\((?:un)?abridged\)\s*", " ", name, flags=re.I).strip()
    subtitle = None
    if ": " in name:
        head, _, tail = name.partition(": ")
        if len(head) >= 3 and len(tail) >= 3:
            name, subtitle = head.strip(), tail.strip()
    return name, subtitle, abridged


def search_itunes(client: HttpClient, parsed: ParsedName, duration: float | None) -> list[Candidate]:
    """iTunes Search API: audiobook listings with descriptions and artwork."""
    term = parsed.title + (f" {parsed.author}" if parsed.author else "")
    url = (
        "https://itunes.apple.com/search"
        f"?term={urllib.parse.quote_plus(term)}&media=audiobook&entity=audiobook&limit=10"
    )
    data = client.get_json(url)
    results: list[Candidate] = []
    for item in (data or {}).get("results") or []:
        raw_name = item.get("collectionName") or item.get("trackName") or ""
        title, subtitle, abridged = _itunes_clean_name(raw_name)
        description = strip_html(item.get("description") or "")

        narrators: list[str] = []
        m = re.search(r"(?:narrated|read)\s+by\s+([A-Z][^.;\n]{2,70})", description, re.I)
        if m:
            narrator = collapse_ws(m.group(1)).strip(" ,")
            if narrator:
                narrators.append(narrator)

        release = item.get("releaseDate") or ""
        year = None
        date = None
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", release)
        if m:
            year = int(m.group(1))
            date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

        artwork = item.get("artworkUrl100") or item.get("artworkUrl60")
        if artwork:
            artwork = artwork.replace("100x100bb", "600x600bb").replace("60x60bb", "600x600bb")

        results.append(
            Candidate(
                source="itunes",
                title=collapse_ws(title),
                subtitle=subtitle,
                authors=[collapse_ws(item.get("artistName") or "")] if item.get("artistName") else [],
                narrators=narrators,
                genres=_clean_genres([item.get("primaryGenreName") or ""]),
                published_year=year,
                published_date=date,
                description=description,
                language="English",
                cover_url=artwork,
                source_url=item.get("collectionViewUrl"),
                abridged=abridged,
            )
        )
    return results


def search_googlebooks(client: HttpClient, parsed: ParsedName, duration: float | None) -> list[Candidate]:
    """Google Books: good for descriptions/ISBNs, heavily rate limited."""
    q = f'intitle:"{parsed.title}"'
    if parsed.author:
        q += f' inauthor:"{parsed.author}"'
    url = (
        "https://www.googleapis.com/books/v1/volumes"
        f"?q={urllib.parse.quote_plus(q)}&maxResults=10&printType=books"
    )
    data = client.get_json(url)
    if not isinstance(data, dict):
        return []
    results: list[Candidate] = []
    for item in data.get("items") or []:
        info = item.get("volumeInfo") or {}
        isbn = None
        for ident in info.get("industryIdentifiers") or []:
            if ident.get("type") == "ISBN_13":
                isbn = ident.get("identifier")
                break
            if ident.get("type") == "ISBN_10" and not isbn:
                isbn = ident.get("identifier")

        date = info.get("publishedDate") or ""
        year = int(date[:4]) if re.match(r"^\d{4}", date) else None
        full_date = date if re.match(r"^\d{4}-\d{2}-\d{2}$", date) else None

        results.append(
            Candidate(
                source="googlebooks",
                title=collapse_ws(info.get("title") or ""),
                subtitle=collapse_ws(info.get("subtitle") or "") or None,
                authors=[collapse_ws(a) for a in (info.get("authors") or [])][:5],
                genres=_clean_genres(info.get("categories") or []),
                published_year=year,
                published_date=full_date,
                publisher=info.get("publisher"),
                description=strip_html(info.get("description") or ""),
                isbn=isbn,
                language=LANGUAGE_NAMES.get(str(info.get("language") or "").lower(), info.get("language")),
                cover_url=(info.get("imageLinks") or {}).get("thumbnail"),
                source_url=info.get("infoLink"),
            )
        )
    return results


SOURCES: dict[str, Any] = {
    "librivox": search_librivox,
    "openlibrary": search_openlibrary,
    "itunes": search_itunes,
    "googlebooks": search_googlebooks,
}
DEFAULT_SOURCES = ("librivox", "openlibrary", "itunes", "googlebooks")


# --------------------------------------------------------------------------- #
# Audio probing / existing metadata detection
# --------------------------------------------------------------------------- #

def _which_ffprobe() -> str | None:
    exe = shutil.which("ffprobe")
    if exe:
        return exe
    for candidate in (
        Path(os.environ.get("FFPROBE_PATH", "")),
        Path("C:/ffmpeg/bin/ffprobe.exe"),
        Path("/usr/bin/ffprobe"),
        Path("/usr/local/bin/ffprobe"),
    ):
        if candidate.is_file():
            return str(candidate)
    return None


_FFPROBE = None


def probe_audio(path: Path) -> dict[str, Any]:
    """Return {'duration': float|None, 'tags': {lower: value}} using ffprobe."""
    global _FFPROBE
    if _FFPROBE is None:
        _FFPROBE = _which_ffprobe() or ""
    if not _FFPROBE:
        return {"duration": None, "tags": {}}
    cmd = [
        _FFPROBE, "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return {"duration": None, "tags": {}}
    if proc.returncode != 0 or not proc.stdout:
        return {"duration": None, "tags": {}}
    try:
        data = json.loads(proc.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return {"duration": None, "tags": {}}

    tags: dict[str, str] = {}
    raw_tags = (data.get("format") or {}).get("tags") or {}
    for key, value in raw_tags.items():
        if isinstance(value, str):
            tags[key.lower()] = value.strip()

    duration = None
    try:
        duration = float((data.get("format") or {}).get("duration") or 0) or None
    except (TypeError, ValueError):
        duration = None

    return {"duration": duration, "tags": tags}


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def check_existing_metadata(dirs: Sequence[Path]) -> tuple[bool, str]:
    """
    Look for local metadata Audiobookshelf would read.

    Returns (exists, short description). A metadata.json that only contains a
    title (i.e. written by a previous, unmatched run) counts as *partial* so the
    book is retried on the next run.
    """
    seen: set[Path] = set()
    for directory in dirs:
        if directory in seen or not directory.is_dir():
            continue
        seen.add(directory)

        md = directory / "metadata.json"
        if md.is_file():
            data = _read_json(md)
            if isinstance(data, dict):
                if isinstance(data.get("metadata"), dict):  # legacy nested format
                    data = data["metadata"]
                if str(data.get("_absmetaStatus") or "") == "fallback":
                    return False, "filename-only metadata from an earlier run (retrying)"
                title = str(data.get("title") or "").strip()
                rich = bool(data.get("authors") or data.get("description") or data.get("narrators"))
                if title and rich:
                    return True, f"metadata.json already in {directory.name}"
                if title:
                    return False, "partial metadata.json (title only, retrying)"

        for pattern in ("*.opf", "*.nfo"):
            for path in sorted(directory.glob(pattern)):
                try:
                    if path.stat().st_size > 0:
                        return True, f"{path.name} already in {directory.name}"
                except OSError:
                    continue

        for name in ("desc.txt", "reader.txt"):
            path = directory / name
            if path.is_file() and path.stat().st_size > 0:
                # Only a description: still worth fetching title/author.
                return False, f"{name} present in {directory.name} (fetching title/author)"
    return False, ""


def has_embedded_metadata(files: Sequence[Path]) -> tuple[bool, str]:
    """True when the audio file already carries a usable title + artist tag."""
    for path in files[:2]:
        info = probe_audio(path)
        tags = info["tags"]
        title = tags.get("title") or tags.get("track") or ""
        artist = tags.get("artist") or tags.get("album_artist") or tags.get("author") or ""
        if title and artist and not re.fullmatch(r"[\d\s]+", title):
            return True, f"embedded tags: {truncate(title, 60)} / {truncate(artist, 40)}"
    return False, ""


# --------------------------------------------------------------------------- #
# Metadata file writers
# --------------------------------------------------------------------------- #

def _caps_ratio(value: str) -> float:
    words = [w for w in value.split() if any(ch.isalpha() for ch in w)]
    if not words:
        return 0.0
    return sum(1 for w in words if w[:1].isupper()) / len(words)


def build_metadata(
    parsed: ParsedName,
    cand: Candidate | None,
    *,
    fallback: bool = False,
) -> dict[str, Any]:
    """
    Build an Audiobookshelf metadata.json payload.

    Key set / types match server/utils/generators/abmetadataGenerator.js:
    unknown keys are dropped and wrong types are rejected by ABS, so we only
    emit the documented keys, in ABS order.
    """
    metadata: dict[str, Any] = {}

    title = (cand.title if cand else "") or parsed.title
    if cand and parsed.title and norm_text(cand.title) == norm_text(parsed.title):
        # Prefer the file name's capitalisation when the source is sentence case.
        if _caps_ratio(cand.title) < 0.5 <= _caps_ratio(parsed.title):
            title = parsed.title
    if title:
        metadata["title"] = collapse_ws(title)

    subtitle = (cand.subtitle if cand else None) or parsed.subtitle
    if subtitle:
        metadata["subtitle"] = collapse_ws(subtitle)

    authors = select_authors(parsed.author, cand.authors if cand else [])
    if not authors and parsed.author:
        authors = [parsed.author]
    if authors:
        metadata["authors"] = authors

    if cand:
        if cand.narrators:
            metadata["narrators"] = cand.narrators[:8]
        if cand.series:
            metadata["series"] = cand.series[:4]
        if cand.genres:
            metadata["genres"] = cand.genres[:8]

    year = (cand.published_year if cand else None) or parsed.year
    if parsed.year and cand and cand.published_year:
        # File names usually cite the original publication year; a modern
        # reprint record should not override it.
        if abs(cand.published_year - parsed.year) > 5:
            year = parsed.year
    if year:
        metadata["publishedYear"] = str(year)

    if cand and cand.published_date:
        metadata["publishedDate"] = cand.published_date

    if cand and cand.publisher:
        metadata["publisher"] = collapse_ws(cand.publisher)

    if not fallback and cand and cand.description:
        metadata["description"] = cand.description.strip()

    if cand and cand.isbn:
        metadata["isbn"] = cand.isbn
    if cand and cand.asin:
        metadata["asin"] = cand.asin
    if cand and cand.language:
        metadata["language"] = cand.language

    if cand and cand.explicit:
        metadata["explicit"] = True
    if cand and cand.abridged is not None:
        metadata["abridged"] = cand.abridged

    if fallback:
        # Unknown keys are ignored by Audiobookshelf's parser; this marker just
        # lets a later run know the file still needs a real match.
        metadata["_absmetaStatus"] = "fallback"

    return metadata


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _looks_like_image(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def download_cover(client: HttpClient, url: str, target_dir: Path) -> str | None:
    """Download a cover next to metadata.json (Audiobookshelf picks it up)."""
    data = client.get_bytes(url)
    if not data or len(data) < 2048:
        return None
    ext = _looks_like_image(data)
    if not ext:
        return None
    target_dir.mkdir(parents=True, exist_ok=True)
    for old in target_dir.glob("cover.*"):
        try:
            old.unlink()
        except OSError:
            pass
    path = target_dir / f"cover{ext}"
    path.write_bytes(data)
    return path.name


# --------------------------------------------------------------------------- #
# Layout: where do audio + sidecars go?
# --------------------------------------------------------------------------- #

@dataclass
class Book:
    """One audiobook, made of one or more files."""
    files: list[Path]
    parsed: ParsedName
    source_dir: Path

    @property
    def label(self) -> str:
        return self.parsed.title or self.files[0].stem


def discover_files(input_dir: Path, extensions: set[str]) -> list[Path]:
    found: list[Path] = []
    for root, dirnames, filenames in os.walk(input_dir):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and not d.startswith("@")]
        for name in sorted(filenames):
            if name.startswith(".") or name.startswith("~$"):
                continue
            path = Path(root) / name
            if path.suffix.lower() in extensions:
                found.append(path)
    return sorted(found)


def group_into_books(files: Sequence[Path], input_dir: Path) -> list[Book]:
    """
    Group audio files into books.

    - Files in a sub-folder are one book (tracks of the same audiobook).
    - Files directly in the input root are one book each, keyed by parsed title.
    - A video twin (e.g. .webm next to an .mp3) of the same title is dropped.
    """
    buckets: dict[str, list[Path]] = {}
    for path in files:
        parent = path.parent
        if parent != input_dir:
            key = f"dir::{parent}"
        else:
            parsed = parse_audio_filename(path.stem)
            key = f"title::{norm_text(parsed.title) or path.stem.lower()}"
        buckets.setdefault(key, []).append(path)

    books: list[Book] = []
    for key, paths in buckets.items():
        # Drop video duplicates when the same title also exists as audio.
        by_title: dict[str, list[Path]] = {}
        for path in paths:
            by_title.setdefault(norm_text(parse_audio_filename(path.stem).title), []).append(path)

        kept: list[Path] = []
        for _, group in by_title.items():
            audio = [p for p in group if p.suffix.lower() in AUDIO_EXTENSIONS]
            kept.extend(audio if audio else group)

        kept.sort(key=lambda p: p.name)
        first = kept[0]
        parsed = parse_audio_filename(first.stem)
        books.append(Book(files=kept, parsed=parsed, source_dir=first.parent))

    books.sort(key=lambda b: (b.parsed.author or "", b.parsed.title.lower()))
    return books


def plan_target_dir(book: Book, args: argparse.Namespace, taken: dict[Path, Book]) -> Path:
    """Compute the directory that will hold the sidecars (and audio, in library layout)."""
    if args.layout == "in-place":
        return book.source_dir

    if args.layout == "mirror":
        try:
            rel = book.source_dir.relative_to(args.input_dir)
        except ValueError:
            rel = Path()
        return args.output_dir / rel

    # layout == library: <Output>/<Author>/<Title>
    base = args.output_dir
    if book.parsed.author:
        base = base / safe_name(book.parsed.author)
    candidate = base / safe_name(book.parsed.title or book.files[0].stem)

    if candidate in taken and taken[candidate] is not book:
        suffix = book.parsed.year or "2"
        candidate = base / f"{safe_name(book.parsed.title)} ({suffix})"
        counter = 2
        while candidate in taken and taken[candidate] is not book:
            candidate = base / f"{safe_name(book.parsed.title)} ({suffix}-{counter})"
            counter += 1

    taken[candidate] = book
    return candidate


def link_audio(src: Path, dest: Path, mode: str) -> str:
    """Place the audio file in the library folder: hardlink -> symlink -> copy."""
    if dest.exists():
        try:
            s, d = src.stat(), dest.stat()
            if (s.st_ino, s.st_dev) == (d.st_ino, d.st_dev):
                return "linked"
            if d.st_size != s.st_size:
                dest.unlink()
            else:
                return "exists"
        except OSError:
            pass

    dest.parent.mkdir(parents=True, exist_ok=True)

    if mode == "copy":
        shutil.copy2(src, dest)
        return "copied"
    if mode == "symlink":
        try:
            os.symlink(src, dest)
            return "symlinked"
        except OSError:
            shutil.copy2(src, dest)
            return "copied"
    if mode == "none":
        return "skipped"

    try:
        os.link(src, dest)
        return "linked"
    except OSError:
        try:
            os.symlink(src, dest)
            return "symlinked"
        except OSError:
            shutil.copy2(src, dest)
            return "copied"


# --------------------------------------------------------------------------- #
# Per-book processing
# --------------------------------------------------------------------------- #

@dataclass
class BookResult:
    book: str
    parsed: dict[str, Any]
    status: str            # written | skipped | fallback | failed | planned
    target: str = ""
    source: str = ""
    score: float = 0.0
    matched_title: str = ""
    cover: bool = False
    audio: list[str] = field(default_factory=list)
    message: str = ""
    needs_review: bool = False
    candidates: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "book": self.book,
            "parsed": self.parsed,
            "status": self.status,
            "target": self.target,
            "source": self.source,
            "score": round(self.score, 3),
            "matchedTitle": self.matched_title,
            "cover": self.cover,
            "audio": self.audio,
            "message": self.message,
            "needsReview": self.needs_review,
            "candidates": self.candidates,
        }


def gather_candidates(
    client: HttpClient,
    parsed: ParsedName,
    duration: float | None,
    sources: Sequence[str],
    workers: int,
) -> list[Candidate]:
    def run(name: str) -> list[Candidate]:
        fn = SOURCES[name]
        try:
            return fn(client, parsed, duration) or []
        except Exception as exc:  # noqa: BLE001 - a broken source must not kill the run
            Log.detail(f"    {name}: error {type(exc).__name__}: {exc}")
            return []

    candidates: list[Candidate] = []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(sources)))) as pool:
        for result in pool.map(run, sources):
            candidates.extend(result)

    for cand in candidates:
        cand.score = score_candidate(parsed, cand, duration)
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates


def process_book(
    book: Book,
    args: argparse.Namespace,
    client: HttpClient,
    taken: dict[Path, Book],
) -> BookResult:
    target_dir = plan_target_dir(book, args, taken)
    result = BookResult(
        book=book.label,
        parsed=book.parsed.as_dict(),
        status="failed",
        target=str(target_dir),
    )

    check_dirs = [target_dir]
    if book.source_dir not in check_dirs:
        check_dirs.append(book.source_dir)

    exists, reason = check_existing_metadata(check_dirs)
    duration = None
    if not exists:
        embedded, embed_reason = has_embedded_metadata(book.files)
        if embedded:
            exists, reason = True, embed_reason
        duration = probe_audio(book.files[0])["duration"]

    if exists and not args.force:
        result.status = "skipped"
        result.message = reason or "metadata already present"
        return result

    if args.offline:
        candidates: list[Candidate] = []
    else:
        candidates = gather_candidates(client, book.parsed, duration, args.sources, args.workers)

    best = candidates[0] if candidates and candidates[0].score >= args.min_score else None

    for cand in candidates[:3]:
        Log.detail(
            f"      {cand.source:<12} {cand.score:.2f}  {truncate(cand.title, 60)}"
            f"{'  (' + truncate(cand.authors[0], 28) + ')' if cand.authors else ''}"
        )

    fallback = best is None
    metadata = build_metadata(book.parsed, best, fallback=fallback)

    if best and not book.parsed.author:
        # Title-only match: nothing on disk says who wrote it, so flag for review.
        result.needs_review = True

    if args.dry_run:
        result.status = "planned"
        result.source = best.source if best else "filename"
        result.score = best.score if best else 0.0
        result.matched_title = metadata.get("title", "")
        result.message = "dry run - nothing written"
        result.candidates = [c.as_dict() for c in candidates[:3]]
        return result

    # ---- write sidecars ---------------------------------------------------- #
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        write_json(target_dir / "metadata.json", metadata)
    except OSError as exc:
        result.status = "failed"
        result.message = f"cannot write metadata: {exc}"
        return result

    result.status = "fallback" if fallback else "written"
    result.source = best.source if best else "filename"
    result.score = best.score if best else 0.0
    result.matched_title = metadata.get("title", "")
    result.candidates = [c.as_dict() for c in candidates[:3]]

    if fallback:
        result.message = (
            f"no confident match (best {candidates[0].score:.2f} < {args.min_score}) - "
            "wrote filename-derived metadata"
            if candidates else "no source returned results - wrote filename-derived metadata"
        )
        Log.warn(f"  {result.message}")

    # ---- cover ------------------------------------------------------------- #
    if not args.no_covers:
        existing_covers = list(target_dir.glob("cover.*"))
        if existing_covers and not args.force:
            result.cover = True
        else:
            # Fall back to the next-best candidate that has artwork.
            cover_url = None
            for cand in candidates:
                if cand.cover_url and cand.score >= max(args.min_score, best.score - 0.15 if best else 1.0):
                    cover_url = cand.cover_url
                    break
            if cover_url:
                result.cover = bool(download_cover(client, cover_url, target_dir))

    # ---- audio ------------------------------------------------------------- #
    if args.layout == "library":
        single = len(book.files) == 1
        for index, src in enumerate(book.files, start=1):
            if args.keep_audio_names:
                name = src.name
            elif single:
                name = f"{safe_name(book.parsed.title or src.stem)}{src.suffix.lower()}"
            else:
                stem = _clean_title(src.stem) or src.stem
                name = f"{index:02d} - {safe_name(stem)}{src.suffix.lower()}"
            dest = target_dir / name
            if args.audio_mode == "none":
                result.audio.append("skipped")
                continue
            try:
                how = link_audio(src, dest, args.audio_mode)
                result.audio.append(how)
                if how == "copied" and args.audio_mode == "hardlink":
                    _warn_copy_fallback()
            except OSError as exc:
                result.audio.append(f"error: {exc}")

    return result


_COPY_WARNED = False


def _warn_copy_fallback() -> None:
    """Explain once why audio was copied instead of hard linked."""
    global _COPY_WARNED
    if _COPY_WARNED:
        return
    _COPY_WARNED = True
    Log.warn(
        "hard links are not permitted here, so audio was copied (uses extra disk space). "
        "Use --audio-mode symlink to link instead."
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fetch_audiobook_metadata.py",
        description=(
            "Fetch audiobook metadata from open sources and write Audiobookshelf-ready "
            "metadata.json sidecars. Existing metadata is detected and skipped."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  %(prog)s -i ./audiobooks -o ./abs-library\n"
            "  %(prog)s -i ./audiobooks -o ./metadata --layout in-place\n"
            "  %(prog)s -i ./audiobooks -o ./abs-library --dry-run -v\n"
            "  %(prog)s -i ./audiobooks -o ./abs-library --force --no-covers\n"
        ),
    )
    parser.add_argument(
        "-i", "--input", default=os.environ.get("ABS_AUDIOBOOKS_DIR", "./audiobooks"),
        help="folder containing audiobook files (default: $ABS_AUDIOBOOKS_DIR or ./audiobooks)",
    )
    parser.add_argument(
        "-o", "--output", default=os.environ.get("ABS_METADATA_DIR", "./abs-library"),
        help="folder where metadata/library output is written (default: $ABS_METADATA_DIR or ./abs-library)",
    )
    parser.add_argument(
        "--layout", choices=("library", "mirror", "in-place"), default="library",
        help=(
            "library: <output>/<Author>/<Title>/ with sidecars + linked audio (default); "
            "mirror: same tree as input, sidecars only; "
            "in-place: write sidecars next to the source audio"
        ),
    )
    parser.add_argument(
        "--audio-mode", choices=("hardlink", "copy", "symlink", "none"), default="hardlink",
        help="how audio is placed in library layout (default: hardlink, falls back to copy)",
    )
    parser.add_argument(
        "--keep-audio-names", action="store_true",
        help="keep original file names instead of renaming to '<Title>.<ext>'",
    )
    parser.add_argument(
        "--sources", default=",".join(DEFAULT_SOURCES),
        help=f"comma separated sources to query (default: {','.join(DEFAULT_SOURCES)})",
    )
    parser.add_argument(
        "--min-score", type=float, default=0.60,
        help="minimum match score 0..1 to accept an online result (default: 0.60)",
    )
    parser.add_argument(
        "--extensions", default=None,
        help="comma separated extensions to scan (default: common audio + video containers)",
    )
    parser.add_argument("--force", action="store_true", help="ignore existing metadata and refetch")
    parser.add_argument("--dry-run", action="store_true", help="show matches without writing anything")
    parser.add_argument("--no-covers", action="store_true", help="do not download cover images")
    parser.add_argument("--no-cache", action="store_true", help="bypass the API response cache")
    parser.add_argument("--offline", action="store_true", help="skip all network calls (filename only)")
    parser.add_argument("--workers", type=int, default=4, help="parallel source requests per book (default: 4)")
    parser.add_argument("--limit", type=int, default=0, help="only process the first N books")
    parser.add_argument("--only", default=None, help="only process books whose file name matches this glob")
    parser.add_argument("--report", default=None, help="path for the JSON report (default: <output>/_fetch-report.json)")
    parser.add_argument("--no-report", action="store_true", help="do not write a JSON report")
    parser.add_argument("-v", "--verbose", action="store_true", help="show candidate matches per book")
    parser.add_argument("-q", "--quiet", action="store_true", help="only print errors and the summary")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _resolve(path_value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path_value))).resolve()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    Log.quiet = args.quiet
    Log.verbose = args.verbose

    if not 0.0 <= args.min_score <= 1.0:
        parser.error("--min-score must be between 0 and 1")

    available = [s.strip().lower() for s in args.sources.split(",") if s.strip()]
    unknown = [s for s in available if s not in SOURCES]
    if unknown:
        parser.error(f"unknown source(s): {', '.join(unknown)} (choose from {', '.join(SOURCES)})")
    args.sources = available

    if args.extensions:
        extensions = {
            e.strip().lower() if e.strip().startswith(".") else "." + e.strip().lower()
            for e in args.extensions.split(",") if e.strip()
        }
    else:
        extensions = DEFAULT_EXTENSIONS

    args.input_dir = _resolve(args.input)
    args.output_dir = _resolve(args.output)

    if not args.input_dir.is_dir():
        Log.error(f"input folder not found: {args.input_dir}")
        return 2

    if args.layout == "in-place":
        Log.info(f"Input      : {args.input_dir}")
        Log.info("Output     : same folders as input (in-place sidecars)")
    else:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        Log.info(f"Input      : {args.input_dir}")
        Log.info(f"Output     : {args.output_dir}")

    files = discover_files(args.input_dir, extensions)
    if not files:
        Log.error(
            f"no audio files found in {args.input_dir} "
            f"(looked for: {', '.join(sorted(extensions))})"
        )
        return 2

    books = group_into_books(files, args.input_dir)
    if args.only:
        import fnmatch
        pattern = args.only.lower()
        books = [b for b in books if fnmatch.fnmatch(b.files[0].name.lower(), pattern)]
    if args.limit and args.limit > 0:
        books = books[: args.limit]

    if not args.offline:
        cache_dir = None if args.no_cache else (args.output_dir / ".cache" if args.layout != "in-place" else args.input_dir / ".metadata-cache")
        client = HttpClient(cache_dir)
    else:
        client = HttpClient(None)

    Log.info(f"Layout     : {args.layout}")
    Log.info(f"Sources    : {', '.join(args.sources) if args.sources else '(none)'}")
    Log.info(f"Books      : {len(books)} (from {len(files)} files)")
    Log.info("")

    taken: dict[Path, Book] = {}
    results: list[BookResult] = []
    started = time.time()

    for index, book in enumerate(books, start=1):
        if not args.quiet:
            print(f"[{index}/{len(books)}] {truncate(book.label, 70)}", flush=True)
        try:
            result = process_book(book, args, client, taken)
        except Exception as exc:  # noqa: BLE001 - keep the batch going
            result = BookResult(
                book=book.label, parsed=book.parsed.as_dict(), status="failed",
                message=f"{type(exc).__name__}: {exc}",
            )
        results.append(result)

        if not args.quiet:
            icons = {"written": "ok", "skipped": "skip", "fallback": "warn", "planned": "plan", "failed": "FAIL"}
            detail = result.message or (f"{result.source} {result.score:.2f}" if result.source else "")
            extras = []
            if result.cover:
                extras.append("cover")
            if result.audio:
                extras.append(result.audio[0])
            if result.needs_review:
                extras.append("review: no author in file name")
            suffix = f" [{'/'.join(extras)}]" if extras else ""
            print(f"    -> {icons.get(result.status, result.status):<4} {truncate(detail, 70)}{suffix}", flush=True)

    elapsed = time.time() - started

    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1

    Log.info("")
    Log.info("Summary")
    Log.info(
        "  written={written} skipped={skipped} fallback={fallback} failed={failed} planned={planned}".format(
            written=counts.get("written", 0),
            skipped=counts.get("skipped", 0),
            fallback=counts.get("fallback", 0),
            failed=counts.get("failed", 0),
            planned=counts.get("planned", 0),
        )
    )
    if not args.offline:
        Log.info(f"  http requests={client.requests} cache hits={client.cache_hits} failures={len(client.failures)}")
        if args.verbose:
            for failure in client.failures[:10]:
                Log.detail(f"    {failure}")
    Log.info(f"  elapsed={elapsed:.1f}s")

    if args.layout != "in-place" and not args.dry_run:
        Log.info("")
        Log.info(f"Add this folder as a library in Audiobookshelf: {args.output_dir}")
        Log.info("  Settings -> Libraries -> Add Library -> Books -> Folder = the path above")

    if counts.get("fallback"):
        Log.info("")
        Log.info(f"  {counts['fallback']} book(s) got filename-only metadata; fix with --min-score or edit in ABS.")

    review = sum(1 for r in results if r.needs_review)
    if review:
        Log.info("")
        Log.info(f"  {review} book(s) matched on title only (no author in the file name) - review those in ABS.")

    if not args.no_report and not args.dry_run:
        report_path = Path(args.report).expanduser().resolve() if args.report else (
            args.output_dir / "_fetch-report.json" if args.layout != "in-place"
            else args.input_dir / "_fetch-report.json"
        )
        report = {
            "tool": "fetch_audiobook_metadata.py",
            "version": __version__,
            "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "input": str(args.input_dir),
            "output": str(args.output_dir),
            "layout": args.layout,
            "sources": args.sources,
            "summary": {**counts, "total": len(results)},
            "books": [r.as_dict() for r in results],
        }
        try:
            write_json(report_path, report)
            Log.info(f"  report: {report_path}")
        except OSError as exc:
            Log.warn(f"could not write report: {exc}")

    return 1 if counts.get("failed") else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, OSError):
        pass
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
