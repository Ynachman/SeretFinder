"""Thin OpenAI embeddings client (plain ``requests``; no extra dependency)."""
from __future__ import annotations

import requests

from .config import get_settings
from .models import EMBEDDING_DIMENSIONS

OPENAI_EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"


class EmbeddingsUnavailable(RuntimeError):
    """Raised when embeddings can't be produced (no key, or the API failed)."""


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Return one embedding per input text, in order."""
    settings = get_settings()
    if not settings.has_openai_api_key:
        raise EmbeddingsUnavailable("OPENAI_API_KEY is not configured (set it in init.env).")
    if not texts:
        return []
    try:
        resp = requests.post(
            OPENAI_EMBEDDINGS_URL,
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            json={"model": settings.openai_embedding_model, "input": texts},
            timeout=60,
        )
        resp.raise_for_status()
        data = sorted(resp.json()["data"], key=lambda d: d["index"])
    except (requests.RequestException, KeyError, ValueError) as exc:
        # Deliberately do not include the request (it carries the API key).
        raise EmbeddingsUnavailable(f"OpenAI embeddings request failed: {type(exc).__name__}") from exc
    vectors = [d["embedding"] for d in data]
    if any(len(v) != EMBEDDING_DIMENSIONS for v in vectors):
        raise EmbeddingsUnavailable(
            f"Model returned unexpected dimensions; expected {EMBEDDING_DIMENSIONS}."
        )
    return vectors


def embed_text(text: str) -> list[float]:
    return embed_texts([text])[0]
