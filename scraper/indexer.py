"""Async Playwright scraper for ``seret.co.il`` movie showtimes.

Flow
----
1. Navigate to the movies index page and collect every movie-page URL.
2. Open each movie page and extract the raw Hebrew title.
3. Open the on-page showtimes modal (falling back to the raw AJAX endpoint)
   and parse cinema listings, dates, and raw showtime strings.
4. Normalize everything into :class:`ShowtimeRecord` objects that map onto the
   ``movies`` / ``cinemas`` / ``showtimes`` database schemas.

Robustness
----------
Every selector interaction is wrapped in its own ``try/except`` so that a
single broken layout on Seret (a missing card, an empty modal, a time-out)
never aborts the whole indexing run — the failure is logged and skipped.

Run from the project root:
    python -m scraper.indexer
"""
from __future__ import annotations

import asyncio
import argparse
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from backend.config import get_settings
from .utils import fetch_tmdb_metadata, normalize_hebrew_title, parse_showtime

logger = logging.getLogger(__name__)

#: Realistic desktop user agent; less likely to be flagged by anti-bot rules.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

#: Matches the numeric MID that identifies a movie on seret.co.il, e.g. ``?MID=8471``.
_MID_RE = re.compile(r"[?&]MID=(\d+)", re.IGNORECASE)


def _extract_movie_id(url: str) -> Optional[str]:
    """Return the numeric ``MID`` from a seret.co.il movie URL, or ``None``."""
    match = _MID_RE.search(url or "")
    return match.group(1) if match else None


@dataclass
class ShowtimeRecord:
    """One normalized screening, shaped to match the database schema."""

    movie_title: str  # -> movies.hebrew_title (normalized)
    cinema_name: str  # -> cinemas.name
    city: Optional[str] = None  # -> cinemas.city
    date: Optional[str] = None  # raw Hebrew day label from the site, e.g. "יום ג 7/8"
    showtime: Optional[str] = None  # parsed "HH:MM" -> showtimes.starts_at
    raw_time: Optional[str] = None  # the raw showtime string as scraped
    screen: Optional[str] = None  # -> showtimes.screen (hall number / name)
    booking_url: Optional[str] = None  # -> showtimes.booking_url
    movie_url: Optional[str] = None  # source page, for traceability
    tmdb: Optional[dict[str, Any]] = field(default=None)  # enrichment payload

    def to_dict(self) -> dict[str, Any]:
        """Convert the record to a JSON-serializable dictionary."""
        return asdict(self)


class SeretIndexer:
    """Async Playwright scraper for Seret.co.il movies and their showtimes."""

    BASE_URL = "https://www.seret.co.il"
    DEFAULT_LISTING_URL = f"{BASE_URL}/movies/newmovies.asp"

    # --- Movies index ----------------------------------------------------------
    #: Each movie grid cell on the index page.
    MOVIE_GRID_SELECTOR = "div.posterDsp"
    #: Any anchor pointing at a movie page (poster + title links both match).
    MOVIE_LINK_SELECTOR = "a[href*='s_movies.asp']"

    # --- Movie page --------------------------------------------------------------
    #: Raw Hebrew title lives in the top h1; English title in the following h2.
    MOVIE_TITLE_SELECTOR = "h1.TitWhite22"
    MOVIE_TITLE_EN_SELECTOR = "h2.TitWhite22"
    #: The showtimes trigger (a div button, or a hero button on some layouts).
    SHOWTIMES_BUTTON_SELECTOR = "#stbtn, button.heroBtnOrange"
    #: Container where the AJAX showtimes are injected (inside the modal).
    SHOWTIMES_CONTAINER_SELECTOR = "#showtimes"

    # --- Showtime blocks (shared by the modal and the raw AJAX endpoint) --------
    #: One full cinema card, grouping a cinema's day-by-day showtimes.
    CINEMA_BLOCK_SELECTOR = ".cardGray"
    #: Candidate selectors for the cinema name inside a card (first hit wins).
    CINEMA_NAME_CANDIDATES = [
        "a.TitGreen20",
        ".cinemaname",
        ".cinema-name",
        ".cinemaitem b",
        ".cinemaitem a",
        "h3",
        "b a",
        "strong a",
    ]
    #: A row holding one day's showtimes.
    DAYLINE_SELECTOR = ".dayline"
    #: One showtime slot. Each ``.stbox`` is a single screening: it holds the
    #: clock time (``.hour``), the screen (``span[title="אולם הקרנה"]``) and a
    #: ``title`` attribute carrying the screening date (``DD/MM/YYYY``).
    SHOWTIME_SLOT_SELECTOR = ".stbox"
    #: The screen / hall number inside a showtime slot.
    SCREEN_SELECTOR = 'span[title="אולם הקרנה"]'
    #: Date embedded in each slot's title, e.g. "12/8/2026 | מדובב | שפת הסרט: עברית".
    SLOT_DATE_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b")

    #: The raw AJAX endpoint that powers the on-page showtimes modal.
    SHOWTIMES_AJAX_PATH = "/movies/showTimesAjaxMovies.asp"

    def __init__(
        self,
        headless: bool = True,
        language: str = "he-IL",
        request_timeout_ms: int = 30_000,
        max_movies: Optional[int] = None,
        use_proxy: bool = True,
    ) -> None:
        self.headless = headless
        self.language = language
        self._timeout_ms = request_timeout_ms
        #: Optional cap on the number of movies to index (useful for testing).
        self._max_movies = max_movies
        #: Set to False to bypass the proxy and connect directly (handy when the
        #: ScrapingAnt plan is out of concurrency, or during local development).
        self._use_proxy = use_proxy

        settings = get_settings()
        self._proxy = self._build_proxy_options(settings)
        self._extra_headers = self._build_extra_headers(settings)

        self._playwright: Any = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    # --- Configuration helpers -----------------------------------------------------
    @staticmethod
    def _build_proxy_options(settings: Any) -> Optional[dict[str, str]]:
        """Build a Playwright ``proxy`` dict from ScrapingAnt settings.

        ScrapingAnt "Proxy Mode" authenticates like a standard HTTP proxy, with
        the fixed username ``scrapingant`` and the API key as the password.
        """
        if not settings.has_scrapingant_key:
            return None
        proxy: dict[str, str] = {
            "server": (
                f"http://{settings.scrapingant_proxy_host}:{settings.scrapingant_proxy_port}"
            ),
            "username": settings.scrapingant_proxy_username or "scrapingant",
            "password": settings.scrapingant_proxy_password or settings.scrapingant_api_key,
        }
        logger.info("Routing requests through ScrapingAnt proxy at %s", proxy["server"])
        return proxy

    @staticmethod
    def _build_extra_headers(settings: Any) -> dict[str, str]:
        """Return extra request headers to send on every navigation."""
        headers: dict[str, str] = {}
        if settings.has_scrapingant_key:
            headers["Accept-Language"] = "he-IL,he;q=0.9,en;q=0.8"
        return headers

    # --- Browser lifecycle -------------------------------------------------------
    async def start(self) -> None:
        """Launch Chromium (optionally proxied) and open a fresh page."""
        proxy = self._proxy if self._use_proxy else None
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless, proxy=proxy
        )
        self._context = await self._browser.new_context(
            user_agent=USER_AGENT,
            extra_http_headers=self._extra_headers,
            # ScrapingAnt intercepts TLS on its proxy, so certificate validation
            # must be relaxed whenever the proxy is in use (per their docs).
            ignore_https_errors=bool(proxy),
        )
        self._page = await self._context.new_page()

    async def stop(self) -> None:
        """Tear down the browser and its resources."""
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        self._browser = self._context = self._page = None

    # --- Step 1: collect movie URLs from the index page ---------------------------
    async def collect_movie_links(self, listing_url: str) -> list[str]:
        """Return the unique absolute URLs of every movie card on the index page."""
        if self._page is None:
            raise RuntimeError("Call start() before collecting links")

        try:
            await self._page.goto(listing_url, wait_until="domcontentloaded", timeout=self._timeout_ms)
            # Wait for the movie grid anchors to be present in the DOM. We must
            # wait for "attached", not "visible": the first matching anchor on
            # Seret is a hidden duplicate, so the visible-state wait never fires.
            await self._page.wait_for_selector(
                self.MOVIE_LINK_SELECTOR, state="attached", timeout=self._timeout_ms
            )
        except PlaywrightTimeoutError:
            logger.error("Timed out waiting for the movie grid on %s", listing_url)
            return []
        except PlaywrightError as exc:
            logger.error("Failed to load the movies index %s: %s", listing_url, exc)
            return []

        try:
            hrefs: list[str] = await self._page.eval_on_selector_all(
                self.MOVIE_LINK_SELECTOR,
                "els => els.map(e => e.href).filter(h => h && /s_movies\\.asp/i.test(h))",
            )
        except PlaywrightError as exc:
            logger.error("Could not evaluate movie links on %s: %s", listing_url, exc)
            return []

        unique: list[str] = list(dict.fromkeys(h for h in hrefs if h))
        if self._max_movies is not None:
            unique = unique[: self._max_movies]
        logger.info("Collected %d movie URLs from %s", len(unique), listing_url)
        return unique

    # --- Step 2: per-movie scraping -------------------------------------------------
    async def scrape_movie(self, movie_url: str) -> list[ShowtimeRecord]:
        """Extract one movie's title and showtimes into normalized records.

        Never raises: any failure is logged and an empty list is returned so a
        single broken movie page cannot crash the indexing run.
        """
        if self._page is None:
            raise RuntimeError("Call start() before scraping")
        movie_id = _extract_movie_id(movie_url)
        logger.debug("Scraping movie MID=%s from %s", movie_id, movie_url)

        try:
            await self._page.goto(movie_url, wait_until="domcontentloaded", timeout=self._timeout_ms)
        except PlaywrightError as exc:
            logger.error("Could not load movie page %s: %s", movie_url, exc)
            return []

        hebrew_title = await self._extract_title(self._page, self.MOVIE_TITLE_SELECTOR)
        english_title = await self._extract_title(self._page, self.MOVIE_TITLE_EN_SELECTOR)
        if not hebrew_title:
            logger.warning("No Hebrew title found on %s; using placeholder", movie_url)
            hebrew_title = "Unknown Title"
        hebrew_title = normalize_hebrew_title(hebrew_title)

        # Primary path: the raw AJAX endpoint is what actually powers the on-page
        # modal (the modal only renders a filter shell until a city is picked),
        # so hitting it directly is both faster and more reliable.
        records: list[ShowtimeRecord] = []
        if movie_id:
            records = await self._fetch_showtimes_ajax(movie_id, hebrew_title, movie_url)

        # Fallback path: try the on-page modal in case the AJAX endpoint changes.
        if not records and await self._open_showtimes_modal(self._page):
            records = await self._parse_showtime_blocks(self._page, hebrew_title, movie_url)

        for record in records:
            record.movie_url = movie_url
            if english_title:
                record.tmdb = record.tmdb or {}
                record.tmdb.setdefault("english_title_hint", english_title)

        logger.info(
            "Movie %s (%r): %d showtimes",
            movie_id or "?",
            hebrew_title,
            len(records),
        )
        return records

    async def _open_showtimes_modal(self, page: Page) -> bool:
        """Click the showtimes button and wait for the modal to populate."""
        try:
            button = page.locator(self.SHOWTIMES_BUTTON_SELECTOR).first
            if await button.count() == 0:
                logger.debug("No showtimes button found on this layout.")
                return False
            await button.click(timeout=self._timeout_ms)
        except PlaywrightError as exc:
            logger.debug("Could not click the showtimes button: %s", exc)
            return False

        try:
            await page.wait_for_selector(
                f"{self.SHOWTIMES_CONTAINER_SELECTOR} .stbox, "
                f"{self.SHOWTIMES_CONTAINER_SELECTOR} .cardGray",
                timeout=self._timeout_ms,
            )
        except PlaywrightTimeoutError:
            logger.debug("Showtimes modal did not populate (movie may have no showtimes).")
            return False
        return True

    async def _fetch_showtimes_ajax(
        self, movie_id: str, hebrew_title: str, movie_url: str
    ) -> list[ShowtimeRecord]:
        """Fallback: scrape the raw AJAX showtimes endpoint for a movie."""
        if self._page is None:
            return []
        ajax_url = f"{self.BASE_URL}{self.SHOWTIMES_AJAX_PATH}?MID={movie_id}&d=&s=&v=&st="
        try:
            await self._page.goto(ajax_url, wait_until="domcontentloaded", timeout=self._timeout_ms)
        except PlaywrightError as exc:
            logger.warning("Could not load showtime AJAX for %s: %s", movie_url, exc)
            return []
        return await self._parse_showtime_blocks(self._page, hebrew_title, movie_url)

    # --- Step 3: parse the showtime DOM ---------------------------------------------
    async def _parse_showtime_blocks(
        self, page: Page, hebrew_title: str, movie_url: str
    ) -> list[ShowtimeRecord]:
        """Parse cinema cards (``.cardGray``) into showtime records.

        Each card is parsed independently inside its own ``try/except`` so one
        malformed card only costs that card, never the whole movie.
        """
        records: list[ShowtimeRecord] = []
        try:
            cards = await page.locator(self.CINEMA_BLOCK_SELECTOR).all()
        except PlaywrightError as exc:
            logger.warning("Could not query cinema blocks on %s: %s", movie_url, exc)
            return records

        if not cards:
            logger.debug("No cinema blocks (.cardGray) found for %s", movie_url)
            return records

        for card in cards:
            try:
                records.extend(await self._parse_cinema_card(card, hebrew_title))
            except PlaywrightError as exc:
                logger.warning("Skipping malformed cinema block on %s: %s", movie_url, exc)
                continue
        return records

    async def _parse_cinema_card(self, card: Locator, hebrew_title: str) -> list[ShowtimeRecord]:
        """Parse one cinema card into its individual showtime records."""
        cinema_name = await self._first_text(card, self.CINEMA_NAME_CANDIDATES)
        city = await self._first_text(card, [".cityname"])
        fallback_day_label = await self._first_text(card, [".dayname"])

        daylines = await card.locator(self.DAYLINE_SELECTOR).all()
        if not daylines:
            daylines = [card]  # Fallback: a card without explicit day rows.

        records: list[ShowtimeRecord] = []
        for dayline in daylines:
            day_label = (
                await self._first_text(dayline, [".dayname"]) or fallback_day_label
            )
            try:
                slots = await dayline.locator(self.SHOWTIME_SLOT_SELECTOR).all()
            except PlaywrightError:
                slots = []
            for slot in slots:
                record = await self._build_showtime_record(
                    slot, hebrew_title, cinema_name, city, day_label
                )
                if record is not None:
                    records.append(record)
        return records

    async def _build_showtime_record(
        self,
        slot: Locator,
        hebrew_title: str,
        cinema_name: str,
        city: Optional[str],
        day_label: str,
    ) -> Optional[ShowtimeRecord]:
        """Turn one ``.stbox`` slot into a :class:`ShowtimeRecord` (or ``None``)."""
        # The clock time lives in the dedicated .hour element; fall back to the
        # slot's full text (parse_showtime returns the first clock time found).
        raw_time = await self._first_text(slot, [".hour"])
        raw_time = raw_time or (await slot.inner_text()).strip()
        parsed_time = parse_showtime(raw_time)
        if parsed_time is None:
            logger.debug(
                "Unparseable showtime string %r (movie=%r)", raw_time, hebrew_title
            )
            return None

        # A concrete screening date is embedded in the slot title, e.g. "12/8/2026".
        slot_date = await self._slot_date(slot)
        screen = await self._first_text(slot, [self.SCREEN_SELECTOR])
        return ShowtimeRecord(
            movie_title=hebrew_title,
            cinema_name=cinema_name or "Unknown Cinema",
            city=city or None,
            date=slot_date or day_label or None,
            showtime=parsed_time,
            raw_time=raw_time,
            screen=screen or None,
            booking_url=await self._slot_booking_url(slot),
        )

    async def _slot_date(self, slot: Locator) -> Optional[str]:
        """Return the ``DD/MM/YYYY`` date embedded in the slot's ``title``, if any."""
        try:
            title = await slot.get_attribute("title")
        except PlaywrightError:
            return None
        match = self.SLOT_DATE_RE.search(title or "")
        return match.group(1) if match else None

    async def _slot_booking_url(self, slot: Locator) -> Optional[str]:
        """Return the booking href from a showtime slot, if any."""
        try:
            return await slot.get_attribute("href")
        except PlaywrightError:
            return None

    # --- Small resilient helpers ------------------------------------------------------
    async def _extract_title(self, page: Page, selector: str) -> str:
        """Return the trimmed inner text of the first element matching ``selector``."""
        try:
            element = page.locator(selector).first
            if await element.count():
                text = (await element.inner_text(timeout=2_000)).strip()
                return text
        except PlaywrightError:
            pass
        return ""

    async def _first_text(self, element: Locator, selectors: list[str]) -> str:
        """Return the first non-empty text found among ``selectors`` (or empty)."""
        for selector in selectors:
            try:
                candidate = element.locator(selector).first
                if await candidate.count():
                    text = (await candidate.inner_text(timeout=2_000)).strip()
                    if text:
                        return text
            except PlaywrightError:
                continue
        return ""

    @staticmethod
    def _dedupe(records: list[ShowtimeRecord]) -> list[ShowtimeRecord]:
        """Drop exact duplicates while preserving first-seen order."""
        seen: set[tuple[Any, ...]] = set()
        unique: list[ShowtimeRecord] = []
        for record in records:
            key = (
                record.movie_title,
                record.cinema_name,
                record.city,
                record.date,
                record.showtime,
                record.screen,
                record.booking_url,
            )
            if key in seen:
                continue
            seen.add(key)
            unique.append(record)
        return unique

    # --- Orchestration -------------------------------------------------------------------
    async def run(
        self,
        listing_url: Optional[str] = None,
        output: Optional[Path] = None,
    ) -> list[ShowtimeRecord]:
        """Index movies from the listing page and enrich them with TMDB data."""
        listing_url = listing_url or self.DEFAULT_LISTING_URL
        records: list[ShowtimeRecord] = []
        try:
            await self.start()
            links = await self.collect_movie_links(listing_url)
            # Sequential navigation: a single shared page is not safe to drive
            # concurrently, and per-movie isolation keeps failures contained.
            for link in links:
                records.extend(await self.scrape_movie(link))
        finally:
            await self.stop()

        # Enrich each unique record with TMDB metadata (blocking, done once).
        for record in self._dedupe(records):
            try:
                tmdb = fetch_tmdb_metadata(record.movie_title, language=self.language)
                if tmdb:
                    if record.tmdb:
                        record.tmdb.update(tmdb)
                    else:
                        record.tmdb = tmdb
            except Exception as exc:  # noqa: BLE001 - enrichment must never abort the run
                logger.warning("TMDB enrichment failed for %r: %s", record.movie_title, exc)

        records = self._dedupe(records)
        if output is not None:
            output.write_text(
                json.dumps([r.to_dict() for r in records], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info("Wrote %d showtime records to %s", len(records), output)
        return records


async def main(argv: list[str] | None = None) -> None:
    """CLI entrypoint for the Seret scraper.

    Default mode is a *test scrape*: it opens the movies index, picks the first
    movie page, and scrapes just that one page so the whole pipeline
    (proxy -> page load -> title -> showtimes modal -> records) can be verified
    quickly. Pass ``--movie-url`` to test a specific page, or ``--all`` to run
    the full index.

    The ScrapingAnt residential proxy is enabled automatically whenever
    ``SCRAPINGANT_API_KEY`` is present in ``init.env`` / ``.env``. Pass
    ``--no-proxy`` to connect directly instead.
    """
    parser = argparse.ArgumentParser(
        description="Scrape movie showtimes from seret.co.il."
    )
    parser.add_argument(
        "--movie-url",
        help="Test-scrape this single movie page (default: first movie on the index).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Index every movie on the listing page instead of a single test page.",
    )
    parser.add_argument(
        "--max-movies",
        type=int,
        help="Cap the number of movies to scrape (only used with --all).",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run with a visible browser window (useful for debugging selectors).",
    )
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        help="Bypass the ScrapingAnt proxy and connect directly.",
    )
    parser.add_argument(
        "--output",
        default="test_scrape.json",
        help="Output JSON file path (default: test_scrape.json).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = get_settings()
    if settings.has_scrapingant_key and not args.no_proxy:
        logger.info(
            "ScrapingAnt proxy enabled (SCRAPINGANT_API_KEY found in init.env/.env)."
        )
    elif args.no_proxy:
        logger.warning("--no-proxy given: connecting directly to seret.co.il.")
    else:
        logger.warning(
            "SCRAPINGANT_API_KEY is not configured - scraping without the proxy, "
            "which may get blocked by the site."
        )

    indexer = SeretIndexer(
        headless=not args.headed,
        max_movies=args.max_movies,
        use_proxy=not args.no_proxy,
    )

    if args.all:
        records = await indexer.run(output=Path(args.output))
        logger.info("Total unique showtime records scraped: %d", len(records))
        return

    # --- Single-movie test scrape ------------------------------------------------
    try:
        await indexer.start()
        if args.movie_url:
            movie_url = args.movie_url
        else:
            links = await indexer.collect_movie_links(indexer.DEFAULT_LISTING_URL)
            if not links:
                logger.error(
                    "No movie links found on %s; nothing to test. "
                    "Try --headed to inspect the page.",
                    indexer.DEFAULT_LISTING_URL,
                )
                return
            movie_url = links[0]

        logger.info("Test-scraping single movie page: %s", movie_url)
        records = await indexer.scrape_movie(movie_url)
    finally:
        await indexer.stop()

    output = Path(args.output)
    output.write_text(
        json.dumps([r.to_dict() for r in records], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("Scraped %d showtime records -> %s", len(records), output)

    if records:
        for record in records:
            logger.info(
                "  %s | %s | %s | %s | %s",
                record.cinema_name,
                record.city or "-",
                record.date or "-",
                record.showtime,
                record.screen or "-",
            )
    else:
        logger.warning(
            "No showtime records parsed. If the selectors drifted, run again with "
            "--headed to watch the browser, or pass --movie-url for a specific page."
        )


if __name__ == "__main__":
    asyncio.run(main())
