"""Data-normalization helpers for Hebrew movie text and TMDB enrichment.

Public helpers
--------------
* :func:`normalize_hebrew_title` — canonicalizes a scraped Hebrew title so it
  matches cleanly against TMDB (strips invisible RTL/LTR control characters and
  niqqud, and normalizes quote/dash glyph variants).
* :func:`parse_showtime` — extracts an ``HH:MM`` time from free-form text.
* :func:`parse_showtime_datetime` — combines a Seret date label (Hebrew or
  ``DD/MM``) with an ``HH:MM`` time into a timezone-aware ``datetime``.
* :func:`fetch_tmdb_metadata` — enriches a Hebrew title with English metadata
  (English title, high-res poster/backdrop URLs, overview), falling back to
  placeholder values when the movie cannot be found.

Note: on Windows, timezone resolution of ``Asia/Jerusalem`` requires the
``tzdata`` package (already added to ``requirements.txt``).
"""
from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from backend.config import Settings, get_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Israel timezone (UTC+2 in winter, UTC+3 in summer; DST handled by IANA data).
# ---------------------------------------------------------------------------
try:
    ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")
except ZoneInfoNotFoundError:  # pragma: no cover - Windows fallback
    import tzdata  # noqa: F401  # registers the IANA database for zoneinfo
    ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")

# ---------------------------------------------------------------------------
# TMDB image URLs. "High-res" poster/backdrop widths are used for a polished,
# Letterboxd-style grid.
# ---------------------------------------------------------------------------
TMDB_IMAGE_BASE_URL = "https://image.tmdb.org/t/p"
POSTER_SIZE = "w780"
BACKDROP_SIZE = "w1280"

# Matches a valid "HH:MM" clock time anywhere inside a free-form string.
_TIME_RE = re.compile(r"\b(?P<hour>\d{1,2}):(?P<minute>\d{2})\b")

# Invisible RTL/LTR control characters, zero-width characters, and other
# non-printing format marks that commonly survive scraping of Hebrew text.
_INVISIBLE_CHARS_RE = re.compile(
    "[\u00ad\u061c\u200b-\u200f\u202a-\u202e\u2060\u2066-\u2069\ufeff\ufe0e\ufe0f]"
)
# Hebrew vowel points and cantillation marks (niqqud).
_NIQQUD_RE = re.compile("[\u0591-\u05c7]")
# Hebrew maqaf (־) and en/em dashes are all normalized to a plain hyphen.
_MAQAF_RE = re.compile("\u05be")
_DASH_RE = re.compile("[–—―]")
# Collapse any whitespace run (newlines, tabs, multiple spaces) into one space.
_WHITESPACE_RE = re.compile(r"\s+")
# Stray surrounding quotes/dashes/whitespace that are usually layout artifacts.
_EDGE_PUNCT_RE = re.compile(r"^[\s'\"\-]+|[\s'\"\-]+$")

# Normalize the many apostrophe / quotation glyphs (Hebrew geresh & gershayim,
# plus typographic quotes) onto plain ASCII so queries match TMDB's stored form.
_QUOTE_TRANSLATION = str.maketrans(
    {
        "\u05f3": "'",  # Hebrew geresh (׳)
        "\u05f4": '"',  # Hebrew gershayim (״)
        "\u2018": "'",  # left single quotation mark
        "\u2019": "'",  # right single quotation mark
        "\u201a": "'",  # single low-9 quotation mark
        "\u201b": "'",  # single high-reversed-9 quotation mark
        "\u201c": '"',  # left double quotation mark
        "\u201d": '"',  # right double quotation mark
        "\u201e": '"',  # double low-9 quotation mark
        "\u201f": '"',  # double high-reversed-9 quotation mark
    }
)

# --- Hebrew month names (full forms, longest-first for regex alternation). ----
_MONTHS_HE = {
    "ינואר": 1,
    "פברואר": 2,
    "מרץ": 3,
    "מרס": 3,
    "אפריל": 4,
    "מאי": 5,
    "יוני": 6,
    "יולי": 7,
    "אוגוסט": 8,
    "ספטמבר": 9,
    "אוקטובר": 10,
    "נובמבר": 11,
    "דצמבר": 12,
}
_MONTHS_HE_ALT = sorted(_MONTHS_HE, key=len, reverse=True)

# --- Hebrew weekday names; ``datetime.weekday()`` uses Mon=0 ... Sun=6. -------
_WEEKDAYS_HE = {
    "ראשון": 6,  # Sunday
    "שני": 0,    # Monday
    "שלישי": 1,  # Tuesday
    "רביעי": 2,  # Wednesday
    "חמישי": 3,  # Thursday
    "שישי": 4,   # Friday
    "שבת": 5,    # Saturday
}
# Short day-of-week letters as printed by Seret (Israeli week starts Sunday).
_WEEKDAY_LETTERS_HE = {"א": 6, "ב": 0, "ג": 1, "ד": 2, "ה": 3, "ו": 4}

# Date-label patterns: "7/8", "07.08.2026", "7 באוגוסט 2026", "יום שלישי 7/8".
_DMY_RE = re.compile(
    r"(?P<day>\d{1,2})[/.](?P<month>\d{1,2})(?:[/.](?P<year>\d{2,4}))?"
)
_MONTH_NAME_RE = re.compile(
    r"(?P<day>\d{1,2})\s*ב?(?P<month>" + "|".join(_MONTHS_HE_ALT) + r")\s*(?P<year>\d{4})?"
)
_WEEKDAY_FULL_RE = re.compile(
    r"יום\s*(ראשון|שני|שלישי|רביעי|חמישי|שישי|שבת)|(ראשון|שני|שלישי|רביעי|חמישי|שישי|שבת)"
)
_WEEKDAY_SHORT_RE = re.compile(r"יום\s*([אבגדהו])")


def normalize_hebrew_title(title: str) -> str:
    """Canonicalize a scraped Hebrew title for clean TMDB matching.

    Steps
    -----
    1. NFKC-decompose the text (unifies presentation forms and full-width glyphs).
    2. Map every quote/apostrophe glyph (incl. geresh/gershayim) to plain ASCII.
    3. Convert maqaf and en/em dashes to a regular hyphen.
    4. Remove Hebrew vowel points (niqqud).
    5. Strip invisible RTL/LTR control and zero-width characters.
    6. Collapse whitespace runs and trim stray surrounding quotes/dashes.

    Returns an empty string for falsy input.
    """
    if not title:
        return ""

    text = unicodedata.normalize("NFKC", title)
    text = text.translate(_QUOTE_TRANSLATION)
    text = _MAQAF_RE.sub("-", text)
    text = _DASH_RE.sub("-", text)
    text = _NIQQUD_RE.sub("", text)
    text = _INVISIBLE_CHARS_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return _EDGE_PUNCT_RE.sub("", text)


def parse_showtime(raw: str) -> Optional[str]:
    """Extract a ``HH:MM`` time from a Hebrew showtime string, or ``None``.

    Accepts free-form inputs such as ``"20:30"``, ``"20:30 אולם 4"`` or
    ``"16:15 | הופעות: 20:30"`` and returns the *first* valid time found.
    """
    match = _TIME_RE.search(raw or "")
    if not match:
        return None
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def parse_showtime_datetime(date_str: str, time_str: str) -> datetime:
    """Combine a Seret date label and an ``HH:MM`` time into a timezone-aware datetime.

    ``date_str`` may be any of the forms Seret uses, for example ``"7/8"``,
    ``"07.08.2026"``, ``"יום שלישי 7/8"``, ``"7 באוגוסט 2026"``, ``"היום"``
    (today) or ``"מחר"`` (tomorrow). ``time_str`` is free text that must
    contain an ``HH:MM`` clock time.

    Returns a ``datetime`` in the Israel timezone (``Asia/Jerusalem``).

    Raises :class:`ValueError` when either part cannot be parsed.
    """
    day, month, year, weekday = _parse_date_label(date_str)
    clock = parse_showtime(time_str)
    if clock is None:
        raise ValueError(f"Could not parse showtime string: {time_str!r}")
    hour, minute = (int(part) for part in clock.split(":"))

    year_explicit = year is not None
    if year is None:
        year = datetime.now(ISRAEL_TZ).year

    def build(target_year: int) -> datetime:
        try:
            return datetime(target_year, month, day, hour, minute, tzinfo=ISRAEL_TZ)
        except ValueError as exc:
            raise ValueError(
                f"Invalid date/time: {day:02d}/{month:02d}/{target_year} {clock}"
            ) from exc

    parsed = build(year)
    # A Hebrew weekday that disagrees with the resolved date usually means the
    # listing refers to the following year (e.g. a late-December run).
    if weekday is not None and not year_explicit and parsed.weekday() != weekday:
        try:
            parsed = build(year + 1)
        except ValueError:
            pass
    return parsed


def fetch_tmdb_metadata(hebrew_title: str, language: str = "he-IL") -> dict[str, Any]:
    """Enrich a Hebrew title with TMDB metadata (English title, images, overview).

    Queries TMDB with the normalized Hebrew title, first in the requested
    locale and then in English so translated titles can still be matched.

    Never raises and never returns ``None``: when the movie is not found (or no
    API key is configured) a dictionary of placeholder values is returned, so
    callers can always persist a row.
    """
    settings = get_settings()
    normalized = normalize_hebrew_title(hebrew_title)

    if not settings.has_tmdb_api_key:
        return _placeholder_metadata(normalized, "TMDB API key is not configured")

    # Re-releases and special screenings carry a suffix ("שרק - חגיגות 25 שנה");
    # if the full title has no match, retry with the part before " - ".
    queries = [normalized]
    if " - " in normalized:
        queries.append(normalized.split(" - ", 1)[0].strip())
    for query in queries:
        for lang in (language, "en-US"):
            movie = _tmdb_search(settings, query, lang)
            if movie:
                logger.info("TMDB match for %r (lang=%s): id=%s", query, lang, movie.get("id"))
                return _build_metadata(normalized, movie)

    logger.info("No TMDB match found for %r", normalized)
    return _placeholder_metadata(normalized, "no TMDB match")


# --- Internal helpers ---------------------------------------------------------


def _parse_date_label(text: str) -> tuple[int, int, Optional[int], Optional[int]]:
    """Return ``(day, month, year, weekday)`` from a Seret date label.

    ``year`` is ``None`` when the label does not carry one, and ``weekday`` is
    ``None`` when no Hebrew weekday name is present. Raises :class:`ValueError`
    for unparseable labels.
    """
    text = _INVISIBLE_CHARS_RE.sub("", text or "").strip()

    # 1) Explicit numeric date: "7/8", "07/08", "7/8/26", "07.08.2026".
    match = _DMY_RE.search(text)
    if match:
        year = _expand_year(match.group("year")) if match.group("year") else None
        return int(match.group("day")), int(match.group("month")), year, _weekday_of(text)

    # 2) Hebrew month name: "7 באוגוסט 2026" or "7 באוגוסט".
    match = _MONTH_NAME_RE.search(text)
    if match:
        year = _expand_year(match.group("year")) if match.group("year") else None
        return int(match.group("day")), _MONTHS_HE[match.group("month")], year, _weekday_of(text)

    # 3) Relative labels: "היום" (today), "מחר" (tomorrow), "מחרתיים" (day after).
    today = datetime.now(ISRAEL_TZ).date()
    if "מחרתיים" in text:
        base = today + timedelta(days=2)
    elif "מחר" in text:
        base = today + timedelta(days=1)
    elif "היום" in text:
        base = today
    else:
        raise ValueError(f"Could not parse Hebrew date label: {text!r}")

    return base.day, base.month, base.year, None


def _expand_year(year: str) -> int:
    """Expand a 2-digit year (``26``) to 4 digits (``2026``)."""
    year_int = int(year)
    return 2000 + year_int if year_int < 100 else year_int


def _weekday_of(text: str) -> Optional[int]:
    """Return the ``datetime.weekday()`` index named in ``text``, if any."""
    match = _WEEKDAY_FULL_RE.search(text)
    if match:
        return _WEEKDAYS_HE[match.group(1) or match.group(2)]
    match = _WEEKDAY_SHORT_RE.search(text)
    if match:
        return _WEEKDAY_LETTERS_HE[match.group(1)]
    return None


def _tmdb_search(settings: Settings, query: str, language: str) -> Optional[dict[str, Any]]:
    """Query TMDB's ``/search/movie`` endpoint and return the best result."""
    params: dict[str, Any] = {
        "api_key": settings.tmdb_api_key,
        "query": query,
        "language": language,
        "include_adult": "false",
    }
    try:
        response = requests.get(
            f"{settings.tmdb_base_url}/search/movie", params=params, timeout=15
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        # Type only: the exception text can contain the request URL, which carries the API key.
        logger.warning("TMDB request failed (lang=%s): %s", language, type(exc).__name__)
        return None

    results = (response.json() or {}).get("results") or []
    return results[0] if results else None


def _build_metadata(hebrew_title: str, movie: dict[str, Any]) -> dict[str, Any]:
    """Convert a TMDB search result into our metadata schema with full image URLs."""
    poster_path = movie.get("poster_path")
    backdrop_path = movie.get("backdrop_path")
    return {
        "tmdb_id": movie.get("id"),
        "hebrew_title": hebrew_title,
        "english_title": movie.get("title") or movie.get("original_title") or "",
        "original_title": movie.get("original_title") or "",
        "overview": movie.get("overview") or "",
        "poster_url": _image_url(POSTER_SIZE, poster_path) if poster_path else "",
        "backdrop_url": _image_url(BACKDROP_SIZE, backdrop_path) if backdrop_path else "",
        "vote_average": movie.get("vote_average"),
        "release_date": movie.get("release_date"),
        "found": True,
    }


def _image_url(size: str, path: str) -> str:
    """Build a full TMDB image URL from a size token and a relative path."""
    return f"{TMDB_IMAGE_BASE_URL}/{size}{path}"


def _placeholder_metadata(hebrew_title: str, reason: str) -> dict[str, Any]:
    """Return the metadata dict populated with placeholder values."""
    return {
        "tmdb_id": None,
        "hebrew_title": hebrew_title,
        "english_title": "",
        "original_title": "",
        "overview": "",
        "poster_url": "",
        "backdrop_url": "",
        "vote_average": None,
        "release_date": None,
        "found": False,
        "note": reason,
    }
