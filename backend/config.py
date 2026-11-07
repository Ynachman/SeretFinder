"""Application settings, loaded from environment variables and a local `.env` file.

All configuration values are typed and resolved once at startup through the
cached ``get_settings()`` factory.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed access to every runtime configuration value used by the app."""

    # --- Database ------------------------------------------------------------
    # PostgreSQL connection string (psycopg2 dialect). SeretFinder targets a
    # cloud-hosted Postgres (Neon) rather than a local instance, e.g.:
    #   postgresql+psycopg2://user:password@ep-xyz-pooler.us-east-2.aws.neon.tech/seretfinder?sslmode=require
    # This default is a local fallback only; override it via .env/init.env.
    database_url: str = (
        "postgresql+psycopg2://postgres:postgres@localhost:5432/seretfinder"
    )

    # --- TMDB (The Movie Database) -------------------------------------------
    # API key used to enrich scraped titles with posters, ratings and synopses.
    tmdb_api_key: str = ""
    tmdb_base_url: str = "https://api.themoviedb.org/3"

    # --- OMDb (IMDb rating + Rotten Tomatoes score) --------------------------
    # Free key (1,000 requests/day): https://www.omdbapi.com/apikey.aspx
    omdb_api_key: str = ""

    # --- OpenAI (embeddings for semantic search) -----------------------------
    # Must produce EMBEDDING_DIMENSIONS (1536) vectors, i.e. text-embedding-3-small.
    openai_api_key: str = ""
    openai_embedding_model: str = "text-embedding-3-small"

    # --- ScrapingAnt (residential proxy to bypass anti-bot protection) -------
    # ScrapingAnt "Proxy Mode" authenticates with the API key as the proxy
    # PASSWORD and the fixed username "scrapingant" (host :8080 = HTTP, :443 = HTTPS).
    scrapingant_api_key: str = ""
    scrapingant_proxy_host: str = "proxy.scrapingant.com"
    scrapingant_proxy_port: int = 8080
    scrapingant_proxy_username: str = "scrapingant"
    scrapingant_proxy_password: str = ""

    # --- CORS ------------------------------------------------------------------
    # Comma-separated list of origins allowed to call the API, e.g. the Next.js
    # frontend dev server. Parsed into a list via ``cors_origin_list``.
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    model_config = SettingsConfigDict(
        env_file=("init.env", ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def cors_origin_list(self) -> list[str]:
        """Return ``cors_origins`` as a cleaned list of non-empty origins."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def has_tmdb_api_key(self) -> bool:
        """True when a TMDB API key has been configured."""
        return bool(self.tmdb_api_key)

    @property
    def has_omdb_api_key(self) -> bool:
        """True when an OMDb API key has been configured."""
        return bool(self.omdb_api_key)

    @property
    def has_openai_api_key(self) -> bool:
        """True when an OpenAI API key has been configured."""
        return bool(self.openai_api_key)

    @property
    def has_scrapingant_key(self) -> bool:
        """True when a ScrapingAnt API key has been configured."""
        return bool(self.scrapingant_api_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance (reads ``.env`` once)."""
    return Settings()
