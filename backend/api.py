"""Read-only REST endpoints for movies, cinemas and showtimes."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session, joinedload

from .database import get_db
from .embeddings import EmbeddingsUnavailable, embed_text
from .models import Cinema, Movie, Showtime
from .schemas import (
    CinemaOut,
    MovieOut,
    MovieShowtimeOut,
    NearbyCinemaOut,
    ShowtimeOut,
)

router = APIRouter()

EARTH_RADIUS_KM = 6371.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- Movies -----------------------------------------------------------------
@router.get("/movies", response_model=list[MovieOut], tags=["movies"])
def list_movies(
    q: Optional[str] = Query(None, description="Substring match on Hebrew/English title"),
    now_showing: bool = Query(True, description="Only movies with upcoming showtimes"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[Movie]:
    stmt = select(Movie)
    if now_showing:
        upcoming = select(Showtime.movie_id).where(Showtime.starts_at >= _now())
        stmt = stmt.where(Movie.id.in_(upcoming))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(Movie.hebrew_title.ilike(like) | Movie.english_title.ilike(like))
    stmt = stmt.order_by(Movie.hebrew_title).limit(limit).offset(offset)
    return list(db.scalars(stmt))


@router.get("/movies/search", response_model=list[MovieOut], tags=["movies"])
def semantic_search(
    q: str = Query(..., min_length=2, description="Free-text, e.g. 'funny family film'"),
    now_showing: bool = Query(True, description="Only movies with upcoming showtimes"),
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
) -> list[Movie]:
    """Semantic (vector) search: movies whose embedding is closest to the query."""
    try:
        query_vec = embed_text(q)
    except EmbeddingsUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    stmt = select(Movie).where(Movie.embedding.is_not(None))
    if now_showing:
        upcoming = select(Showtime.movie_id).where(Showtime.starts_at >= _now())
        stmt = stmt.where(Movie.id.in_(upcoming))
    stmt = stmt.order_by(Movie.embedding.cosine_distance(query_vec)).limit(limit)
    return list(db.scalars(stmt))


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
    date_from: Optional[datetime] = Query(None, description="Defaults to now"),
    date_to: Optional[datetime] = None,
    limit: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[Showtime]:
    if db.get(Movie, movie_id) is None:
        raise HTTPException(status_code=404, detail="Movie not found")
    conds = [Showtime.movie_id == movie_id, Showtime.starts_at >= (date_from or _now())]
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
    dlat = func.radians(Cinema.latitude - lat)
    dlng = func.radians(Cinema.longitude - lng)
    a = func.power(func.sin(dlat / 2), 2) + (
        func.cos(func.radians(lat))
        * func.cos(func.radians(Cinema.latitude))
        * func.power(func.sin(dlng / 2), 2)
    )
    distance = (2 * EARTH_RADIUS_KM * func.asin(func.sqrt(func.least(1.0, a)))).label("distance_km")
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
