"""Export the database to one compact ``site/data.json`` for the static web app.

The app (``site/index.html``) loads this file once and does all filtering
(area, day, search, sort) on the device, so it can be hosted for free on any
static host (GitHub Pages) with no server and no cold starts.

Format (short keys keep it small; ~40 KB gzipped for a typical week)::

    {
      "v": 1,
      "generated_at": "2026-11-08T02:10:00+00:00",
      "movies":  [{"id", "he", "en", "poster", "overview", "imdb_id", "imdb", "votes", "rt", "tmdb"}],
      "cinemas": [{"id", "name", "city", "lat", "lng"}],
      "shows":   [[movie_id, cinema_id, start_unix_seconds, screen_or_null], ...]
    }

Run from the project root:
    python -m scraper.export                 # -> site/data.json
    python -m scraper.export --out other.json
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from backend.database import SessionLocal
from backend.models import Cinema, Movie, Showtime

logger = logging.getLogger(__name__)

DEFAULT_OUT = Path("site") / "data.json"
#: Keep shows that started up to this long ago (the app hides past ones itself).
GRACE = timedelta(hours=3)


def _small_poster(url: str | None) -> str | None:
    """TMDB serves w780 by default; w342 is plenty for phones and ~4x smaller."""
    return url.replace("/w780/", "/w342/") if url else None


def build_payload(session: Session) -> dict:
    since = datetime.now(timezone.utc) - GRACE
    shows = (
        session.query(Showtime.movie_id, Showtime.cinema_id, Showtime.starts_at, Showtime.screen)
        .filter(Showtime.starts_at >= since)
        .order_by(Showtime.starts_at)
        .all()
    )
    movie_ids = {s.movie_id for s in shows}
    cinema_ids = {s.cinema_id for s in shows}

    movies = [
        {
            "id": m.id,
            "he": m.hebrew_title,
            "en": m.english_title,
            "poster": _small_poster(m.poster_path),
            "overview": m.overview,
            "imdb_id": m.imdb_id,
            "imdb": m.imdb_rating,
            "votes": m.imdb_votes,
            "rt": m.rt_score,
            "tmdb": round(m.vote_average, 1) if m.vote_average else None,
        }
        for m in session.query(Movie).filter(Movie.id.in_(movie_ids)).order_by(Movie.id)
    ]
    cinemas = [
        {
            "id": c.id,
            "name": c.name,
            "city": c.city,
            "lat": round(c.latitude, 5) if c.latitude is not None else None,
            "lng": round(c.longitude, 5) if c.longitude is not None else None,
        }
        for c in session.query(Cinema).filter(Cinema.id.in_(cinema_ids)).order_by(Cinema.id)
    ]
    return {
        "v": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "movies": movies,
        "cinemas": cinemas,
        "shows": [[s.movie_id, s.cinema_id, int(s.starts_at.timestamp()), s.screen] for s in shows],
    }


def export(out: Path = DEFAULT_OUT) -> dict:
    session = SessionLocal()
    try:
        payload = build_payload(session)
    finally:
        session.close()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(out)  # atomic: the app never sees a half-written file
    logger.info(
        "Exported %d movies, %d cinemas, %d shows -> %s (%.0f KB)",
        len(payload["movies"]), len(payload["cinemas"]), len(payload["shows"]),
        out, out.stat().st_size / 1024,
    )
    return payload


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Export the DB to site/data.json.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    export(args.out)


if __name__ == "__main__":
    main()
