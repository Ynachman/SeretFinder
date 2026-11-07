"""FastAPI application entrypoint with CORS configuration.

Run with: ``uvicorn backend.main:app --reload``
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from . import models  # noqa: F401  (registers models on Base.metadata)
from .api import router as api_router
from .config import get_settings
from .database import Base, engine, enable_vector_extension
from .migrations import apply_migrations

settings = get_settings()
STATIC_DIR = Path(__file__).parent / "static"


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


@app.get("/", include_in_schema=False)
async def root() -> FileResponse:
    """Serve the web UI."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe for deployment health checks."""
    return {"status": "healthy"}
