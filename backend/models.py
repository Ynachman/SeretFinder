"""SQLAlchemy ORM models for Movies, Cinemas, and Showtimes.

All models inherit from :class:`backend.database.Base` and use the modern
SQLAlchemy 2.0 ``Mapped`` / ``mapped_column`` style with full type hints.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base

if TYPE_CHECKING:  # avoid circular imports; only needed for type annotations
    pass

#: Dimensionality of the OpenAI ``text-embedding-3-small`` vectors.
EMBEDDING_DIMENSIONS = 1536


class Movie(Base):
    """A movie, enriched with TMDB metadata and a semantic search vector."""

    __tablename__ = "movies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    tmdb_id: Mapped[Optional[int]] = mapped_column(Integer, unique=True, index=True, nullable=True)
    hebrew_title: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    english_title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    overview: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    poster_path: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    backdrop_path: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    release_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    vote_average: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    #: pgvector embedding produced by the daily enrichment cron job.
    embedding: Mapped[Optional[Vector]] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=True)

    #: All screenings of this movie across every cinema.
    showtimes: Mapped[list["Showtime"]] = relationship(
        back_populates="movie", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Movie id={self.id} hebrew_title={self.hebrew_title!r}>"


class Cinema(Base):
    """A single cinema location, optionally geocoded to lat/long."""

    __tablename__ = "cinemas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    chain: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    city: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    address: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    latitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    longitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    showtimes: Mapped[list["Showtime"]] = relationship(
        back_populates="cinema", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Cinema id={self.id} name={self.name!r}>"


class Showtime(Base):
    """A single screening: one movie at one cinema at a given date/time."""

    __tablename__ = "showtimes"
    __table_args__ = (
        # Prevent duplicate screenings of the same movie in the same screen slot.
        UniqueConstraint("movie_id", "cinema_id", "starts_at", "screen", name="uq_showtime_slot"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    movie_id: Mapped[int] = mapped_column(
        ForeignKey("movies.id", ondelete="CASCADE"), index=True, nullable=False
    )
    cinema_id: Mapped[int] = mapped_column(
        ForeignKey("cinemas.id", ondelete="CASCADE"), index=True, nullable=False
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    screen: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    booking_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    movie: Mapped["Movie"] = relationship(back_populates="showtimes")
    cinema: Mapped["Cinema"] = relationship(back_populates="showtimes")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Showtime id={self.id} movie_id={self.movie_id} starts_at={self.starts_at}>"
