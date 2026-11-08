"""Daily refresh: scrape -> ingest -> geocode -> ratings -> export site/data.json.

Each step is isolated, so a failure in an optional step (geocode, ratings)
is logged and doesn't stop the others. Exit code is non-zero if the scrape
produced nothing, so schedulers can flag a broken scraper.

Run from the project root:
    python -m scraper.refresh
    python -m scraper.refresh --max-movies 5     # quick smoke test
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone

from backend import models  # noqa: F401  (registers tables)
from backend.database import Base, SessionLocal, engine, enable_vector_extension
from backend.migrations import apply_migrations
from backend.models import Showtime

from .indexer import SeretIndexer
from .ingest import ingest_records

logger = logging.getLogger("refresh")


def _step(name: str, session, fn) -> None:
    """Run an optional step; log (never raise) on failure, keeping the session usable."""
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        logger.error("Step %r failed: %s", name, type(exc).__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the full daily refresh.")
    parser.add_argument("--max-movies", type=int, help="Cap movies scraped (testing).")
    parser.add_argument("--proxy", action="store_true", help="Use the ScrapingAnt proxy (off by default).")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    enable_vector_extension()
    Base.metadata.create_all(bind=engine)
    apply_migrations(engine)

    logger.info("1/4 scraping seret.co.il")
    indexer = SeretIndexer(max_movies=args.max_movies, use_proxy=args.proxy)
    records = asyncio.run(indexer.run())
    if not records:
        logger.error("Scrape returned no showtimes; leaving existing data untouched.")
        return 1

    session = SessionLocal()
    try:
        logger.info("2/4 ingesting %d records", len(records))
        ingest_records(records, session)

        # Past showtimes are never shown; drop ones older than a week to keep the DB small.
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        deleted = session.query(Showtime).filter(Showtime.starts_at < cutoff).delete()
        session.commit()
        logger.info("Pruned %d old showtimes", deleted)

        from .geocode import geocode_missing
        from .ratings import update_ratings

        logger.info("3/4 geocoding new cinemas")
        _step("geocode", session, lambda: geocode_missing(session))
        logger.info("4/4 ratings")
        _step("ratings", session, lambda: update_ratings(session))
    finally:
        session.close()

    from .export import export

    export()
    logger.info("Refresh complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
