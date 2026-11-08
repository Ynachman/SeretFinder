"""Fetch external ratings (IMDb, Rotten Tomatoes) and the real English title.

Sources
-------
* **TMDB** ``/movie/{id}?language=en-US&append_to_response=external_ids``:
  the English title and the IMDb id (uses your existing TMDB key).
* **IMDb rating + votes**: IMDb's official daily dataset
  ``title.ratings.tsv.gz`` (https://datasets.imdbws.com, free for personal,
  non-commercial use, ~8 MB, refreshed daily). It covers new releases far
  better and fresher than OMDb, whose cache lags weeks behind.
* **Rotten Tomatoes**: OMDb (needs ``OMDB_API_KEY``). RT has no public API and
  OMDb only knows the Tomatometer for titles RT has reviewed — so most
  Israeli, small or very new releases legitimately have no RT score.

Google user ratings are intentionally not included: Google has no public API
for movie ratings (only for places), and scraping its results violates its ToS.

Run from the project root:
    python -m scraper.ratings            # refresh (RT only if older than 3 days)
    python -m scraper.ratings --all      # refresh everything now
"""
from __future__ import annotations

import argparse
import gzip
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import requests
from sqlalchemy.orm import Session

from backend.config import get_settings
from backend.database import SessionLocal
from backend.models import Movie

from .utils import fetch_tmdb_metadata

logger = logging.getLogger(__name__)

OMDB_URL = "https://www.omdbapi.com/"
IMDB_RATINGS_URL = "https://datasets.imdbws.com/title.ratings.tsv.gz"
#: Local cache of the IMDb dataset (gitignored); re-downloaded when >20h old.
IMDB_CACHE = Path("data") / "title.ratings.tsv.gz"
_HEBREW_RE = re.compile(r"[֐-׿]")


# --- TMDB ---------------------------------------------------------------------
def fetch_tmdb_details(tmdb_id: int) -> Optional[dict[str, Any]]:
    """Return ``{"english_title", "imdb_id"}`` for a TMDB movie, or ``None``."""
    settings = get_settings()
    if not settings.has_tmdb_api_key:
        return None
    try:
        resp = requests.get(
            f"{settings.tmdb_base_url}/movie/{tmdb_id}",
            params={
                "api_key": settings.tmdb_api_key,
                "language": "en-US",
                "append_to_response": "external_ids",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        # Type only: the exception text can contain the request URL (and key).
        logger.warning("TMDB details failed for %s: %s", tmdb_id, type(exc).__name__)
        return None
    title = data.get("title") or ""
    return {
        "english_title": title if title and not _HEBREW_RE.search(title) else None,
        "imdb_id": (data.get("external_ids") or {}).get("imdb_id") or data.get("imdb_id") or None,
    }


# --- IMDb dataset -------------------------------------------------------------
def _download_imdb_dataset() -> Path:
    """Download IMDb's ratings dataset unless the cached copy is fresh."""
    IMDB_CACHE.parent.mkdir(exist_ok=True)
    if IMDB_CACHE.exists() and time.time() - IMDB_CACHE.stat().st_mtime < 20 * 3600:
        return IMDB_CACHE
    tmp = IMDB_CACHE.with_suffix(".part")
    with requests.get(IMDB_RATINGS_URL, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_content(1 << 16):
                fh.write(chunk)
    tmp.replace(IMDB_CACHE)
    logger.info("Downloaded IMDb ratings dataset (%.1f MB)", IMDB_CACHE.stat().st_size / 1e6)
    return IMDB_CACHE


def load_imdb_ratings(imdb_ids: set[str]) -> dict[str, tuple[float, int]]:
    """Return ``{imdb_id: (rating, votes)}`` for the requested ids."""
    if not imdb_ids:
        return {}
    path = _download_imdb_dataset()
    found: dict[str, tuple[float, int]] = {}
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        next(fh, None)  # header: tconst, averageRating, numVotes
        for line in fh:
            tconst, _, rest = line.partition("\t")
            if tconst in imdb_ids:
                rating, _, votes = rest.rstrip("\n").partition("\t")
                found[tconst] = (float(rating), int(votes))
                if len(found) == len(imdb_ids):
                    break
    return found


# --- OMDb (Rotten Tomatoes) -----------------------------------------------------
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
    payload: Optional[dict[str, Any]] = None
    for attempt in range(1, 4):
        try:
            resp = requests.get(
                OMDB_URL,
                params={"i": imdb_id, "apikey": settings.omdb_api_key},
                timeout=15,
            )
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 * attempt)  # free tier is flaky under bursts; back off
                continue
            resp.raise_for_status()
            payload = resp.json()
            break
        except requests.HTTPError as exc:
            # Only the status code: the exception text contains the URL (and key).
            logger.warning("OMDb request failed for %s: HTTP %s", imdb_id, exc.response.status_code)
            return None
        except (requests.RequestException, ValueError) as exc:
            if attempt < 3:
                time.sleep(2 * attempt)
                continue
            logger.warning("OMDb request failed for %s: %s", imdb_id, type(exc).__name__)
            return None
    if payload is None:
        return None
    if payload.get("Error") and "limit" in str(payload["Error"]).lower():
        logger.error("OMDb daily limit reached; stopping Rotten Tomatoes refresh.")
        raise RuntimeError("OMDb request limit reached")
    return parse_omdb(payload)


# --- Orchestration --------------------------------------------------------------
def _match_tmdb(movie: Movie) -> None:
    """Try to give an unmatched movie a TMDB id (and poster/overview)."""
    meta = fetch_tmdb_metadata(movie.hebrew_title)
    if not meta.get("tmdb_id"):
        return
    # Another row may already own this TMDB id (tmdb_id is unique).
    movie.tmdb_id = meta["tmdb_id"]
    movie.poster_path = movie.poster_path or meta.get("poster_url") or None
    movie.backdrop_path = movie.backdrop_path or meta.get("backdrop_url") or None
    movie.overview = movie.overview or meta.get("overview") or None
    movie.vote_average = movie.vote_average or meta.get("vote_average")


def update_ratings(session: Session, max_age_days: Optional[int] = 3) -> int:
    """Fill TMDB/IMDb ids and English titles, then refresh IMDb and RT ratings.

    IMDb ratings are refreshed for every movie on each run (one cheap dataset
    lookup). RT (OMDb) is refreshed only when older than ``max_age_days``;
    ``None`` refreshes it for every movie. Returns the number of movies updated.
    """
    settings = get_settings()
    movies = session.query(Movie).order_by(Movie.id).all()
    taken = {m.tmdb_id for m in movies if m.tmdb_id}

    # 1) ids + English titles from TMDB
    for movie in movies:
        if not movie.tmdb_id:
            before = movie.tmdb_id
            _match_tmdb(movie)
            if movie.tmdb_id in taken:  # duplicate of an existing row; don't collide
                movie.tmdb_id = before
            elif movie.tmdb_id:
                taken.add(movie.tmdb_id)
        needs_title = not movie.english_title or bool(_HEBREW_RE.search(movie.english_title))
        if movie.tmdb_id and (movie.imdb_id is None or needs_title):
            details = fetch_tmdb_details(movie.tmdb_id)
            if details:
                movie.imdb_id = movie.imdb_id or details["imdb_id"]
                if needs_title:
                    movie.english_title = details["english_title"]
    session.commit()

    # 2) IMDb ratings from the official dataset
    with_ids = [m for m in movies if m.imdb_id]
    try:
        imdb = load_imdb_ratings({m.imdb_id for m in with_ids})
    except (requests.RequestException, OSError, ValueError) as exc:
        logger.warning("IMDb dataset unavailable (%s); keeping previous IMDb ratings", type(exc).__name__)
        imdb = None
    updated = set()
    if imdb is not None:
        for movie in with_ids:
            if movie.imdb_id in imdb:
                movie.imdb_rating, movie.imdb_votes = imdb[movie.imdb_id]
                updated.add(movie.id)
        session.commit()

    # 3) Rotten Tomatoes from OMDb
    if settings.has_omdb_api_key:
        cutoff = None if max_age_days is None else datetime.now(timezone.utc) - timedelta(days=max_age_days)
        for movie in with_ids:
            if cutoff and movie.ratings_updated_at and movie.ratings_updated_at > cutoff:
                continue
            try:
                omdb = fetch_omdb(movie.imdb_id)
            except RuntimeError:
                break
            if omdb is None:
                continue
            movie.rt_score = omdb["rt_score"]
            # OMDb's IMDb numbers are only a fallback when the dataset lacks the title.
            if movie.imdb_rating is None and omdb["imdb_rating"] is not None:
                movie.imdb_rating, movie.imdb_votes = omdb["imdb_rating"], omdb["imdb_votes"]
            movie.ratings_updated_at = datetime.now(timezone.utc)
            updated.add(movie.id)
        session.commit()
    else:
        logger.info("OMDB_API_KEY not set: skipping Rotten Tomatoes.")
    return len(updated)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Fetch IMDb / Rotten Tomatoes ratings.")
    parser.add_argument("--all", action="store_true", help="Refresh everything now.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    session = SessionLocal()
    try:
        n = update_ratings(session, None if args.all else 3)
    finally:
        session.close()
    logger.info("Updated ratings for %d movies", n)


if __name__ == "__main__":
    main()
