"""Geocode cinemas (fill ``latitude`` / ``longitude``) via OpenStreetMap Nominatim.

Seret only gives us a cinema name and a city, so for each cinema without
coordinates we try, in order:

1. ``"<cinema name>, <city>, Israel"``  (precise, if OSM knows the venue)
2. ``"<city>, Israel"``                 (city centre; good enough for radius search)

Nominatim's usage policy allows at most 1 request/second and requires an
identifying User-Agent, so lookups are throttled and results for the same
query are cached for the run.

Run from the project root:
    python -m scraper.geocode            # fill in missing coordinates
    python -m scraper.geocode --redo     # re-geocode every cinema
"""
from __future__ import annotations

import argparse
import logging
import re
import time
from typing import Optional

import requests
from sqlalchemy.orm import Session

from backend.database import SessionLocal
from backend.models import Cinema

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "SeretFinder/0.1 (showtime aggregator; personal project)"
#: Rough bounding box of Israel (lon_min, lat_max, lon_max, lat_min); rejects
#: matches that landed in another country.
_VIEWBOX = "34.2,33.4,35.9,29.4"
_MIN_INTERVAL_S = 1.1

_last_call = 0.0
_cache: dict[str, Optional[tuple[float, float]]] = {}


def clean_city(city: Optional[str]) -> str:
    """Turn Seret city labels into something Nominatim understands.

    ``"תל-אביב/יפו"`` -> ``"תל אביב"``; ``"רמת-גן/גבעתיים"`` -> ``"רמת גן"``.
    """
    if not city:
        return ""
    first = re.split(r"[/]", city)[0]
    return first.replace("-", " ").strip()


def _lookup(query: str) -> Optional[tuple[float, float]]:
    """Return ``(lat, lon)`` for ``query`` or ``None`` (throttled + cached)."""
    global _last_call
    if query in _cache:
        return _cache[query]
    wait = _MIN_INTERVAL_S - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={
                "q": query,
                "format": "jsonv2",
                "limit": 1,
                "countrycodes": "il",
                "viewbox": _VIEWBOX,
                "bounded": 1,
                "accept-language": "he",
            },
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
        resp.raise_for_status()
        results = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Geocoding %r failed: %s", query, exc)
        return None  # don't cache failures: may be transient
    finally:
        _last_call = time.monotonic()
    point = (float(results[0]["lat"]), float(results[0]["lon"])) if results else None
    _cache[query] = point
    return point


def geocode_cinema(name: str, city: Optional[str]) -> Optional[tuple[float, float, str]]:
    """Return ``(lat, lon, precision)`` where precision is ``"venue"`` or ``"city"``."""
    city_clean = clean_city(city)
    point = _lookup(f"{name}, {city_clean}, ישראל" if city_clean else f"{name}, ישראל")
    if point:
        return (*point, "venue")
    if city_clean:
        point = _lookup(f"{city_clean}, ישראל")
        if point:
            return (*point, "city")
    return None


def geocode_missing(session: Session, redo: bool = False) -> int:
    """Fill coordinates for cinemas lacking them. Returns the number updated."""
    query = session.query(Cinema)
    if not redo:
        query = query.filter(Cinema.latitude.is_(None) | Cinema.longitude.is_(None))
    updated = 0
    for cinema in query.order_by(Cinema.id):
        found = geocode_cinema(cinema.name, cinema.city)
        if found is None:
            logger.warning("No location found for %r (%s)", cinema.name, cinema.city)
            continue
        cinema.latitude, cinema.longitude, precision = found
        updated += 1
        logger.info("%s (%s) -> %.5f, %.5f [%s]", cinema.name, cinema.city,
                    cinema.latitude, cinema.longitude, precision)
    session.commit()
    return updated


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Geocode cinemas via Nominatim.")
    parser.add_argument("--redo", action="store_true", help="Re-geocode all cinemas.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    session = SessionLocal()
    try:
        n = geocode_missing(session, redo=args.redo)
    finally:
        session.close()
    logger.info("Geocoded %d cinemas", n)


if __name__ == "__main__":
    main()
