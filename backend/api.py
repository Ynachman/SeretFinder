"""Read-only REST endpoints for movies, cinemas and showtimes."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session, joinedload

from .database import get_db
from .models import Cinema, Movie, Showtime
from .schemas import (
    CinemaOut,
    CityOut,
    MovieOut,
    MovieShowtimeOut,
    NearbyCinemaOut,
    ShowtimeOut,
)

router = APIRouter()

EARTH_RADIUS_KM = 6371.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _distance_km(lat: float, lng: float):
    """SQL expression: haversine distance (km) from a point to ``Cinema``."""
    dlat = func.radians(Cinema.latitude - lat)
    dlng = func.radians(Cinema.longitude - lng)
    a = func.power(func.sin(dlat / 2), 2) + (
        func.cos(func.radians(lat))
        * func.cos(func.radians(Cinema.latitude))
        * func.power(func.sin(dlng / 2), 2)
    )
    return 2 * EARTH_RADIUS_KM * func.asin(func.sqrt(func.least(1.0, a)))


def _area(lat: Optional[float], lng: Optional[float], radius_km: Optional[float]):
    """Validate the optional lat/lng/radius trio; return it or ``None``."""
    given = [v is not None for v in (lat, lng, radius_km)]
    if not any(given):
        return None
    if not all(given):
        raise HTTPException(status_code=422, detail="lat, lng and radius_km must be given together")
    return lat, lng, radius_km


def _cinemas_in_area(area):
    """Sub-select of cinema ids within the area (``(lat, lng, radius_km)``)."""
    lat, lng, radius = area
    return select(Cinema.id).where(
        Cinema.latitude.is_not(None),
        Cinema.longitude.is_not(None),
        _distance_km(lat, lng) <= radius,
    )


# --- Movies -----------------------------------------------------------------
@router.get("/movies", response_model=list[MovieOut], tags=["movies"])
def list_movies(
    q: Optional[str] = Query(None, description="Substring match on Hebrew/English title"),
    now_showing: bool = Query(True, description="Only movies with upcoming showtimes"),
    lat: Optional[float] = Query(None, ge=-90, le=90),
    lng: Optional[float] = Query(None, ge=-180, le=180),
    radius_km: Optional[float] = Query(None, gt=0, le=500, description="With lat/lng: only movies showing within this radius"),
    date_from: Optional[datetime] = Query(None, description="Only count/require showtimes from here (default: now)"),
    date_to: Optional[datetime] = Query(None, description="...and up to here"),
    sort: Literal["popular", "rating", "title"] = Query(
        "popular", description="popular = most upcoming screenings (in the area/window)"
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[Movie]:
    area = _area(lat, lng, radius_km)
    start = max(date_from, _now()) if date_from else _now()
    conds = [Showtime.starts_at >= start]
    if date_to:
        conds.append(Showtime.starts_at <= date_to)
    if area:
        conds.append(Showtime.cinema_id.in_(_cinemas_in_area(area)))
    counts = (
        select(Showtime.movie_id, func.count(Showtime.id).label("n"))
        .where(*conds)
        .group_by(Showtime.movie_id)
        .subquery()
    )
    n = func.coalesce(counts.c.n, 0)
    stmt = select(Movie, n).outerjoin(counts, counts.c.movie_id == Movie.id)
    # A location or time window implies "must be showing there / then".
    if now_showing or area or date_from or date_to:
        stmt = stmt.where(counts.c.n > 0)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(Movie.hebrew_title.ilike(like) | Movie.english_title.ilike(like))
    order = {
        "popular": [n.desc(), Movie.imdb_votes.desc().nulls_last()],
        "rating": [Movie.imdb_rating.desc().nulls_last(), Movie.imdb_votes.desc().nulls_last()],
        "title": [],
    }[sort]
    stmt = stmt.order_by(*order, Movie.hebrew_title).limit(limit).offset(offset)
    movies = []
    for movie, count in db.execute(stmt).all():
        movie.upcoming_showtimes = count  # transient, for the response only
        movies.append(movie)
    return movies


@router.get("/movies/{movie_id}", response_model=MovieOut, tags=["movies"])
def get_movie(movie_id: int, db: Session = Depends(get_db)) -> Movie:
    movie = db.get(Movie, movie_id)
    if movie is None:
        raise HTTPException(status_code=404, detail="Movie not found")
    return movie


@router.get(
    "/movies/{movie_id}/showtimes",
    response_model=list[MovieShowtimeOut],
    tags=["movies"],
)
def movie_showtimes(
    movie_id: int,
    city: Optional[str] = None,
    lat: Optional[float] = Query(None, ge=-90, le=90),
    lng: Optional[float] = Query(None, ge=-180, le=180),
    radius_km: Optional[float] = Query(None, gt=0, le=500),
    date_from: Optional[datetime] = Query(None, description="Defaults to now"),
    date_to: Optional[datetime] = None,
    limit: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[Showtime]:
    area = _area(lat, lng, radius_km)
    if db.get(Movie, movie_id) is None:
        raise HTTPException(status_code=404, detail="Movie not found")
    conds = [Showtime.movie_id == movie_id, Showtime.starts_at >= (date_from or _now())]
    if area:
        conds.append(Showtime.cinema_id.in_(_cinemas_in_area(area)))
    if date_to:
        conds.append(Showtime.starts_at <= date_to)
    stmt = (
        select(Showtime)
        .join(Showtime.cinema)
        .options(joinedload(Showtime.cinema))
        .where(and_(*conds))
        .order_by(Showtime.starts_at)
        .limit(limit)
    )
    if city:
        stmt = stmt.where(Cinema.city.ilike(city))
    return list(db.scalars(stmt))


# --- Cities -----------------------------------------------------------------
@router.get("/cities", response_model=list[CityOut], tags=["cinemas"])
def list_cities(db: Session = Depends(get_db)) -> list[CityOut]:
    """Cities that have geocoded cinemas, with their centre (mean cinema position)."""
    rows = db.execute(
        select(
            Cinema.city,
            func.avg(Cinema.latitude),
            func.avg(Cinema.longitude),
            func.count(Cinema.id),
        )
        .where(Cinema.city.is_not(None), Cinema.latitude.is_not(None), Cinema.longitude.is_not(None))
        .group_by(Cinema.city)
        .order_by(Cinema.city)
    ).all()
    return [CityOut(city=c, latitude=la, longitude=lo, cinemas=n) for c, la, lo, n in rows]


# --- Cinemas ----------------------------------------------------------------
@router.get("/cinemas", response_model=list[CinemaOut], tags=["cinemas"])
def list_cinemas(
    city: Optional[str] = None,
    chain: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[Cinema]:
    stmt = select(Cinema)
    if city:
        stmt = stmt.where(Cinema.city.ilike(city))
    if chain:
        stmt = stmt.where(Cinema.chain.ilike(chain))
    return list(db.scalars(stmt.order_by(Cinema.name).limit(limit).offset(offset)))


@router.get("/cinemas/nearby", response_model=list[NearbyCinemaOut], tags=["cinemas"])
def nearby_cinemas(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    radius_km: float = Query(10, gt=0, le=200),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[NearbyCinemaOut]:
    """Cinemas within ``radius_km`` of a point, nearest first (haversine)."""
    distance = _distance_km(lat, lng).label("distance_km")
    stmt = (
        select(Cinema, distance)
        .where(Cinema.latitude.is_not(None), Cinema.longitude.is_not(None))
        .order_by(distance)
        .limit(limit)
    )
    rows = db.execute(stmt).all()
    return [
        NearbyCinemaOut(
            **CinemaOut.model_validate(c, from_attributes=True).model_dump(),
            distance_km=round(d, 2),
        )
        for c, d in rows
        if d <= radius_km
    ]


@router.get("/cinemas/{cinema_id}", response_model=CinemaOut, tags=["cinemas"])
def get_cinema(cinema_id: int, db: Session = Depends(get_db)) -> Cinema:
    cinema = db.get(Cinema, cinema_id)
    if cinema is None:
        raise HTTPException(status_code=404, detail="Cinema not found")
    return cinema


@router.get(
    "/cinemas/{cinema_id}/showtimes", response_model=list[ShowtimeOut], tags=["cinemas"]
)
def cinema_showtimes(
    cinema_id: int,
    date_from: Optional[datetime] = Query(None, description="Defaults to now"),
    date_to: Optional[datetime] = None,
    limit: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[Showtime]:
    if db.get(Cinema, cinema_id) is None:
        raise HTTPException(status_code=404, detail="Cinema not found")
    conds = [Showtime.cinema_id == cinema_id, Showtime.starts_at >= (date_from or _now())]
    if date_to:
        conds.append(Showtime.starts_at <= date_to)
    stmt = select(Showtime).where(and_(*conds)).order_by(Showtime.starts_at).limit(limit)
    return list(db.scalars(stmt))
