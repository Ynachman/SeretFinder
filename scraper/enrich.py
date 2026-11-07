"""Compute ``Movie.embedding`` vectors for semantic search.

Run from the project root:
    python -m scraper.enrich            # embed movies that have no embedding yet
    python -m scraper.enrich --redo     # re-embed every movie
"""
from __future__ import annotations

import argparse
import logging

from sqlalchemy.orm import Session

from backend.database import SessionLocal
from backend.embeddings import embed_texts
from backend.models import Movie

logger = logging.getLogger(__name__)
BATCH_SIZE = 64


def movie_text(movie: Movie) -> str:
    """The text that represents a movie in embedding space."""
    parts = [movie.hebrew_title, movie.english_title, movie.overview]
    return "\n".join(p for p in parts if p)


def embed_missing(session: Session, redo: bool = False) -> int:
    """Embed movies lacking a vector. Returns the number updated."""
    query = session.query(Movie)
    if not redo:
        query = query.filter(Movie.embedding.is_(None))
    movies = query.order_by(Movie.id).all()
    updated = 0
    for i in range(0, len(movies), BATCH_SIZE):
        batch = movies[i : i + BATCH_SIZE]
        vectors = embed_texts([movie_text(m) for m in batch])
        for movie, vec in zip(batch, vectors):
            movie.embedding = vec
        session.commit()
        updated += len(batch)
        logger.info("Embedded %d/%d movies", updated, len(movies))
    return updated


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Embed movies for semantic search.")
    parser.add_argument("--redo", action="store_true", help="Re-embed all movies.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    session = SessionLocal()
    try:
        n = embed_missing(session, redo=args.redo)
    finally:
        session.close()
    logger.info("Embedded %d movies", n)


if __name__ == "__main__":
    main()
