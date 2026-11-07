"""Fetch external ratings (IMDb, Rotten Tomatoes) for movies.

* The IMDb id comes from TMDB (``/movie/{id}/external_ids``) and needs only the
  TMDB key you already have.
* The IMDb rating/votes and the Rotten Tomatoes Tomatometer come from OMDb
  (https://www.omdbapi.com, free key: 1,000 requests/day). Without an
  ``OMDB_API_KEY`` only the IMDb id is stored (the UI still links to IMDb).

Google user ratings are intentionally not included: Google has no public API
for movie ratings (only for places), and scraping its results violates its ToS.

Run from the project root:
    python -m scraper.ratings            # refresh ratings older than 3 days
    python -m scraper.ratings --all      # refresh every movie now
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests
from sqlalchemy.orm import Session

from backend.config import get_settings
from backend.database import SessionLocal
from backend.models import Movie

logger = logging.getLogger(__name__)

OMDB_URL = "https://www.omdbapi.com/"


def fetch_imdb_id(tmdb_id: int) -> Optional[str]:
    """Return the IMDb id (``tt...``) for a TMDB movie id, or ``None``."""
    settings = get_settings()
    if not settings.has_tmdb_api_key:
        return None
    try:
        resp = requests.get(
            f"{settings.tmdb_base_url}/movie/{tmdb_id}/external_ids",
            params={"api_key": settings.tmdb_api_key},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("imdb_id") or None
    except (requests.RequestException, ValueError) as exc:
        # Log only the exception type: its message can contain the request URL (and key).
        logger.warning("TMDB external_ids failed for %s: %s", tmdb_id, type(exc).__name__)
        return None


def parse_omdb(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract ``imdb_rating``, ``imdb_votes`` and ``rt_score`` from an OMDb payload."""
    out: dict[str, Any] = {"imdb_rating": None, "imdb_votes": None, "rt_score": None}
    if payload.get("Response") == "False":
        return out
    try:
        out["imdb_rating"] = float(payload["imdbRating"])
    except (KeyError, ValueError, TypeError):
        pass
    try:
        out["imdb_votes"] = int(str(payload["imdbVotes"]).replace(",", ""))
    except (KeyError, ValueError, TypeError):
        pass
    for rating in payload.get("Ratings") or []:
        if rating.get("Source") == "Rotten Tomatoes":
            try:
                out["rt_score"] = int(str(rating["Value"]).rstrip("%"))
            except (KeyError, ValueError):
                pass
    return out


def fetch_omdb(imdb_id: str) -> Optional[dict[str, Any]]:
    """Return parsed OMDb ratings for ``imdb_id`` or ``None`` on request failure."""
    settings = get_settings()
    try:
        resp = requests.get(
            OMDB_URL,
            params={"i": imdb_id, "apikey": settings.omdb_api_key},
            timeout=15,
        )
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("OMDb request failed for %s: %s", imdb_id, type(exc).__name__)
        return None
    if payload.get("Error") and "limit" in str(payload["Error"]).lower():
        logger.error("OMDb daily limit reached; stopping ratings refresh.")
        raise RuntimeError("OMDb request limit reached")
    return parse_omdb(payload)


def update_ratings(session: Session, max_age_days: Optional[int] = 3) -> int:
    """Fill IMDb ids and refresh ratings. Returns the number of movies updated.

    ``max_age_days=None`` refreshes every movie regardless of age.
    """
    settings = get_settings()
    cutoff = None if max_age_days is None else datetime.now(timezone.utc) - timedelta(days=max_age_days)
    updated = 0
    for movie in session.query(Movie).order_by(Movie.id).all():
        if movie.imdb_id is None and movie.tmdb_id:
            movie.imdb_id = fetch_imdb_id(movie.tmdb_id)
        if not movie.imdb_id:
            continue
        if not settings.has_omdb_api_key:
            updated += 1  # only the id was stored
            continue
        if cutoff and movie.ratings_updated_at and movie.ratings_updated_at > cutoff:
            continue
        try:
            ratings = fetch_omdb(movie.imdb_id)
        except RuntimeError:
            break
        if ratings is None:
            continue
        movie.imdb_rating = ratings["imdb_rating"]
        movie.imdb_votes = ratings["imdb_votes"]
        movie.rt_score = ratings["rt_score"]
        movie.ratings_updated_at = datetime.now(timezone.utc)
        updated += 1
    session.commit()
    return updated


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Fetch IMDb / Rotten Tomatoes ratings.")
    parser.add_argument("--all", action="store_true", help="Refresh every movie now.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not get_settings().has_omdb_api_key:
        logger.warning("OMDB_API_KEY not set: only IMDb ids will be stored, no ratings.")
    session = SessionLocal()
    try:
        n = update_ratings(session, None if args.all else 3)
    finally:
        session.close()
    logger.info("Updated ratings for %d movies", n)


if __name__ == "__main__":
    main()
