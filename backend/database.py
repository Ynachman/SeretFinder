"""Database engine, session factory, and declarative base.

Uses PostgreSQL with the ``pgvector`` extension to host both relational
showtime data and high-dimensional semantic vectors for AI search.
"""
from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings

settings = get_settings()


class Base(DeclarativeBase):
    """Base class for every ORM model (see :mod:`backend.models`)."""


engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,  # recycle stale connections before each use
    pool_size=10,
    max_overflow=20,
    future=True,
)

#: Factory producing new database sessions for a request / worker context.
SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding one database session per request."""
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def enable_vector_extension() -> None:
    """Create the pgvector extension if it is not already installed.

    Requires the PostgreSQL user to hold ``CREATE`` privilege on the database.
    """
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()
