"""Tiny additive schema migrations.

``Base.metadata.create_all`` creates missing *tables* but never adds columns to
existing ones. Each statement here is idempotent (``IF NOT EXISTS``), so it is
safe to run on every startup / ingest.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

_STATEMENTS = [
    "ALTER TABLE movies ADD COLUMN IF NOT EXISTS imdb_id VARCHAR(20)",
    "ALTER TABLE movies ADD COLUMN IF NOT EXISTS imdb_rating DOUBLE PRECISION",
    "ALTER TABLE movies ADD COLUMN IF NOT EXISTS imdb_votes INTEGER",
    "ALTER TABLE movies ADD COLUMN IF NOT EXISTS rt_score INTEGER",
    "ALTER TABLE movies ADD COLUMN IF NOT EXISTS ratings_updated_at TIMESTAMP WITH TIME ZONE",
]


def apply_migrations(engine: Engine) -> None:
    """Bring an existing database up to the current model definitions."""
    with engine.begin() as conn:
        for stmt in _STATEMENTS:
            conn.execute(text(stmt))
