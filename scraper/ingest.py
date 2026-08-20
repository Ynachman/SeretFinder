"""Load scraped :class:`ShowtimeRecord` objects into Postgres.

Upserts ``Movie`` / ``Cinema`` rows and inserts ``Showtime`` rows, skipping
records that fail to parse or violate the DB schema so one bad record can't
abort a whole ingestion run (matching the scraper's own failure philosophy).

Run from the project root:
    python -m scraper.ingest test_scrape.json
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy.orm import Session

from backend.database import SessionLocal
from backend.models import Cinema, Movie, Showtime

from .indexer import ShowtimeRecord
from .utils import parse_showtime_datetime

logger = logging.getLogger(__name__)


def ingest_records(records: Iterable[ShowtimeRecord], session: Session) -> int:
    """Upsert ``records`` into the database. Returns the count of showtimes written.

    Never raises for a single bad record: parsing/DB errors on one record are
    logged and skipped so the rest of the batch still lands.
    """
    written = 0
    for record in records:
        try:
            movie = _get_or_create_movie(session, record)
            cinema = _get_or_create_cinema(session, record)
            starts_at = parse_showtime_datetime(record.date or "", record.showtime or "")
        except (ValueError, TypeError) as exc:
            logger.warning(
                "Skipping record (movie=%r, cinema=%r): %s",
                record.movie_title, record.cinema_name, exc,
            )
            continue

        existing = (
            session.query(Showtime)
            .filter_by(
                movie_id=movie.id,
                cinema_id=cinema.id,
                starts_at=starts_at,
                screen=record.screen,
            )
            .one_or_none()
        )
        if existing is not None:
            existing.booking_url = record.booking_url or existing.booking_url
            continue

        session.add(
            Showtime(
                movie=movie,
                cinema=cinema,
                starts_at=starts_at,
                screen=record.screen,
                booking_url=record.booking_url,
            )
        )
        written += 1

    session.commit()
    logger.info("Ingested %d new showtimes", written)
    return written


def _get_or_create_movie(session: Session, record: ShowtimeRecord) -> Movie:
    """Find or create the ``Movie`` for ``record``, applying any TMDB enrichment."""
    tmdb: dict[str, Any] = record.tmdb or {}
    tmdb_id = tmdb.get("tmdb_id")

    movie: Movie | None = None
    if tmdb_id:
        movie = session.query(Movie).filter_by(tmdb_id=tmdb_id).one_or_none()
    if movie is None:
        movie = session.query(Movie).filter_by(hebrew_title=record.movie_title).one_or_none()

    if movie is None:
        movie = Movie(hebrew_title=record.movie_title)
        session.add(movie)

    if tmdb_id:
        movie.tmdb_id = tmdb_id
    if tmdb.get("english_title"):
        movie.english_title = tmdb["english_title"]
    elif tmdb.get("english_title_hint") and not movie.english_title:
        movie.english_title = tmdb["english_title_hint"]
    if tmdb.get("overview"):
        movie.overview = tmdb["overview"]
    if tmdb.get("poster_url"):
        movie.poster_path = tmdb["poster_url"]
    if tmdb.get("backdrop_url"):
        movie.backdrop_path = tmdb["backdrop_url"]
    if tmdb.get("release_date"):
        movie.release_date = tmdb["release_date"]
    if tmdb.get("vote_average") is not None:
        movie.vote_average = tmdb["vote_average"]

    session.flush()  # assign movie.id for the Showtime lookup below
    return movie


def _get_or_create_cinema(session: Session, record: ShowtimeRecord) -> Cinema:
    """Find or create the ``Cinema`` named in ``record`` (unique by name + city)."""
    cinema = (
        session.query(Cinema)
        .filter_by(name=record.cinema_name, city=record.city)
        .one_or_none()
    )
    if cinema is None:
        cinema = Cinema(name=record.cinema_name, city=record.city)
        session.add(cinema)
        session.flush()  # assign cinema.id for the Showtime lookup below
    return cinema


def ingest_json_file(path: Path) -> int:
    """Load a JSON file of scraped records (as written by ``scraper.indexer``) into the DB."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    records = [ShowtimeRecord(**item) for item in raw]
    session = SessionLocal()
    try:
        return ingest_records(records, session)
    finally:
        session.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Ingest a scraped showtimes JSON file into Postgres.")
    parser.add_argument("path", type=Path, help="Path to a JSON file written by scraper.indexer")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    count = ingest_json_file(args.path)
    logger.info("Done: %d showtimes ingested from %s", count, args.path)


if __name__ == "__main__":
    main()
