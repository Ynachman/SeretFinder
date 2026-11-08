"""FastAPI app: the JSON API plus a local preview of the static web app.

The published app is fully static (``site/`` + ``site/data.json``, see
``scraper.export``); this server just serves the same folder for local use.

Run with: ``uvicorn backend.main:app --reload``  ->  http://127.0.0.1:8000/
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import models  # noqa: F401  (registers models on Base.metadata)
from .api import router as api_router
from .config import get_settings
from .database import Base, engine, enable_vector_extension
from .migrations import apply_migrations

settings = get_settings()
SITE_DIR = Path(__file__).resolve().parent.parent / "site"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Run once at startup: enable pgvector and create any missing tables."""
    enable_vector_extension()
    Base.metadata.create_all(bind=engine)
    apply_migrations(engine)
    yield


app = FastAPI(
    title="SeretFinder",
    description="Israel Movie Showtime Aggregator API",
    version="0.1.0",
    lifespan=lifespan,
)

# --- CORS -------------------------------------------------------------------
# Restrict cross-origin access to the configured frontend origins.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(api_router)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe for deployment health checks."""
    return {"status": "healthy"}


# The static web app (registered last so API routes take precedence).
# Run ``python -m scraper.export`` first to create site/data.json.
app.mount("/", StaticFiles(directory=SITE_DIR, html=True), name="site")
