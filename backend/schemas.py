"""Pydantic response schemas for the read API."""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class MovieOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tmdb_id: Optional[int] = None
    hebrew_title: str
    english_title: Optional[str] = None
    overview: Optional[str] = None
    poster_path: Optional[str] = None
    backdrop_path: Optional[str] = None
    release_date: Optional[date] = None
    vote_average: Optional[float] = None


class CinemaOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    chain: Optional[str] = None
    city: Optional[str] = None
    address: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None


class NearbyCinemaOut(CinemaOut):
    distance_km: float


class ShowtimeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    movie_id: int
    cinema_id: int
    starts_at: datetime
    screen: Optional[str] = None
    booking_url: Optional[str] = None


class MovieShowtimeOut(ShowtimeOut):
    """A showtime together with its cinema (used on the movie detail page)."""

    cinema: CinemaOut
