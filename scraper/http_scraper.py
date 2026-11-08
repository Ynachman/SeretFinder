"""Browser-free scraper for seret.co.il (plain HTTP + BeautifulSoup).

Why: Cloudflare in front of seret.co.il shows a bot check to *headless
browsers* (Playwright) on cloud machines, but serves the very same pages
normally to plain HTTP requests. Plain HTTP is also much faster and lighter on
the site, so this is the default scraper; :mod:`scraper.indexer` (Playwright)
remains as a fallback (``python -m scraper.refresh --browser``).

Flow per movie (sequential, with a small polite delay):
1. ``/movies/newmovies.asp``                     -> movie ids (MID)
2. ``/movies/s_movies.asp?MID=…``                -> Hebrew + English title
3. ``/movies/showTimesAjaxMovies.asp?MID=…``     -> cinemas, days, showtimes

Run from the project root (writes JSON, no database needed):
    python -m scraper.http_scraper --max-movies 3 --output test_scrape.json
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Iterable, Optional

import requests
from bs4 import BeautifulSoup, Tag

from .indexer import USER_AGENT, SeretIndexer, ShowtimeRecord, _extract_movie_id
from .utils import fetch_tmdb_metadata, normalize_hebrew_title, parse_showtime

logger = logging.getLogger(__name__)

BASE_URL = SeretIndexer.BASE_URL
LISTING_URL = SeretIndexer.DEFAULT_LISTING_URL
AJAX_URL = BASE_URL + SeretIndexer.SHOWTIMES_AJAX_PATH + "?MID={mid}&d=&s=&v=&st="
MOVIE_URL = BASE_URL + "/movies/s_movies.asp?MID={mid}"
SLOT_DATE_RE = SeretIndexer.SLOT_DATE_RE
#: Pause between requests: be a gentle visitor (≈50 movies × 2 pages ≈ 1 min).
DELAY_S = 0.4


_CHARSET_RE = re.compile(rb"""<meta[^>]+charset=["']?([\w-]+)""", re.I)
#: Hebrew mis-decoded with a Cyrillic/Central-European codepage looks like this.
_MOJIBAKE_RE = re.compile(r"[Ѐ-ӿ]")


def _charset(resp: requests.Response) -> str:
    header = re.search(r"charset=([\w-]+)", resp.headers.get("content-type", ""), re.I)
    if header:
        return header.group(1)
    meta = _CHARSET_RE.search(resp.content[:5000])
    return meta.group(1).decode("ascii") if meta else "windows-1255"


class ScrapeBlocked(RuntimeError):
    """The site answered with a bot-check page instead of content."""


class SeretHttpScraper:
    def __init__(self, max_movies: Optional[int] = None, delay_s: float = DELAY_S) -> None:
        self.max_movies = max_movies
        self.delay_s = delay_s
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
        })
        self._last = 0.0

    # --- HTTP ---------------------------------------------------------------------
    def _get(self, url: str) -> BeautifulSoup:
        wait = self.delay_s - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        last_exc: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                resp = self.session.get(url, timeout=30)
                self._last = time.monotonic()
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(3 * attempt)
                    continue
                resp.raise_for_status()
                break
            except requests.RequestException as exc:
                last_exc = exc
                self._last = time.monotonic()
                if attempt < 3:
                    time.sleep(3 * attempt)
                    continue
                raise
        else:  # pragma: no cover - loop always breaks or raises
            raise last_exc or RuntimeError("unreachable")
        # Seret serves Hebrew as windows-1255. The showtime (AJAX) responses
        # declare it only in the HTTP header, with no <meta>, and charset
        # *guessing* gets ~20% of them wrong (Hebrew -> Cyrillic garbage), so
        # never guess: header charset, else the page's <meta>, else cp1255.
        soup = BeautifulSoup(resp.content, "html.parser", from_encoding=_charset(resp))
        title = soup.title.get_text(strip=True) if soup.title else ""
        if "Just a moment" in title or soup.select_one("#challenge-form, .cf-turnstile"):
            raise ScrapeBlocked(f"bot check served for {url}")
        return soup

    # --- Pages ----------------------------------------------------------------------
    def movie_ids(self) -> list[str]:
        soup = self._get(LISTING_URL)
        mids = [_extract_movie_id(a.get("href", "")) for a in soup.select(SeretIndexer.MOVIE_LINK_SELECTOR)]
        unique = list(dict.fromkeys(m for m in mids if m))
        if self.max_movies is not None:
            unique = unique[: self.max_movies]
        logger.info("Collected %d movie ids from the index", len(unique))
        return unique

    def movie_titles(self, mid: str) -> tuple[str, str]:
        soup = self._get(MOVIE_URL.format(mid=mid))
        he = soup.select_one(SeretIndexer.MOVIE_TITLE_SELECTOR)
        en = soup.select_one(SeretIndexer.MOVIE_TITLE_EN_SELECTOR)
        return (he.get_text(" ", strip=True) if he else "", en.get_text(" ", strip=True) if en else "")

    def showtimes(self, mid: str, title: str, movie_url: str) -> list[ShowtimeRecord]:
        soup = self._get(AJAX_URL.format(mid=mid))
        records: list[ShowtimeRecord] = []
        for card in soup.select(SeretIndexer.CINEMA_BLOCK_SELECTOR):
            try:
                records.extend(self._parse_card(card, title, movie_url))
            except Exception as exc:  # noqa: BLE001 - one bad card must not lose the movie
                logger.warning("Skipping malformed cinema card for MID=%s: %s", mid, exc)
        return records

    # --- Parsing (mirrors SeretIndexer's Playwright parsing) ---------------------------------
    @staticmethod
    def _text(el: Optional[Tag]) -> str:
        return el.get_text(" ", strip=True) if el else ""

    def _first_text(self, root: Tag, selectors: Iterable[str]) -> str:
        for sel in selectors:
            text = self._text(root.select_one(sel))
            if text:
                return text
        return ""

    def _parse_card(self, card: Tag, title: str, movie_url: str) -> list[ShowtimeRecord]:
        cinema = self._first_text(card, SeretIndexer.CINEMA_NAME_CANDIDATES) or "Unknown Cinema"
        city = self._text(card.select_one(".cityname"))
        if not city:
            section = card.find_parent("section", class_="mvf-city")
            city = self._text(section.select_one("h3")) if section else ""
        fallback_day = self._text(card.select_one(".dayname"))
        daylines = card.select(SeretIndexer.DAYLINE_SELECTOR) or [card]

        records = []
        for dayline in daylines:
            day_label = self._text(dayline.select_one(".dayname")) or fallback_day
            for slot in dayline.select(SeretIndexer.SHOWTIME_SLOT_SELECTOR):
                raw = self._text(slot.select_one(".hour")) or self._text(slot)
                parsed = parse_showtime(raw)
                if parsed is None:
                    continue
                m = SLOT_DATE_RE.search(slot.get("title") or "")
                href = slot.get("href") if slot.name == "a" else None
                if _MOJIBAKE_RE.search(f"{title} {cinema} {city}"):
                    raise ValueError(f"garbled text (encoding problem): {cinema!r}")
                records.append(ShowtimeRecord(
                    movie_title=title,
                    cinema_name=cinema,
                    city=city or None,
                    date=(m.group(1) if m else None) or day_label or None,
                    showtime=parsed,
                    raw_time=raw,
                    screen=self._text(slot.select_one(SeretIndexer.SCREEN_SELECTOR)) or None,
                    booking_url=href,
                    movie_url=movie_url,
                ))
        return records

    # --- Orchestration -------------------------------------------------------------
    def run(self) -> list[ShowtimeRecord]:
        records: list[ShowtimeRecord] = []
        for mid in self.movie_ids():
            url = MOVIE_URL.format(mid=mid)
            try:
                he, en = self.movie_titles(mid)
                if not he:
                    logger.warning("No Hebrew title for MID=%s; skipping", mid)
                    continue
                title = normalize_hebrew_title(he)
                movie_records = self.showtimes(mid, title, url)
            except ScrapeBlocked:
                raise
            except Exception as exc:  # noqa: BLE001 - one movie must not stop the run
                logger.warning("MID=%s failed: %s", mid, type(exc).__name__)
                continue
            for r in movie_records:
                if en:
                    r.tmdb = {"english_title_hint": en}
            logger.info("Movie %s (%r): %d showtimes", mid, title, len(movie_records))
            records.extend(movie_records)
        return enrich_with_tmdb(SeretIndexer._dedupe(records))


def enrich_with_tmdb(records: list[ShowtimeRecord], language: str = "he-IL") -> list[ShowtimeRecord]:
    """Attach TMDB metadata, one lookup per distinct title."""
    cache: dict[str, dict[str, Any]] = {}
    for r in records:
        try:
            if r.movie_title not in cache:
                cache[r.movie_title] = fetch_tmdb_metadata(r.movie_title, language=language)
            meta = cache[r.movie_title]
        except Exception as exc:  # noqa: BLE001 - enrichment must never abort the run
            logger.warning("TMDB enrichment failed for %r: %s", r.movie_title, type(exc).__name__)
            continue
        if meta:
            r.tmdb = {**(r.tmdb or {}), **meta}
    return records


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Scrape seret.co.il over plain HTTP.")
    parser.add_argument("--max-movies", type=int)
    parser.add_argument("--output", default="test_scrape.json")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    records = SeretHttpScraper(max_movies=args.max_movies).run()
    Path(args.output).write_text(
        json.dumps([r.to_dict() for r in records], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("Scraped %d showtime records -> %s", len(records), args.output)


if __name__ == "__main__":
    main()
