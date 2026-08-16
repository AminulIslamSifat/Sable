"""Gemini API embedding backend for memory search."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("sable.memory_search")

_CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "system"
_GEMINI_KEYS_PATH = _CACHE_DIR / ".gemini_api_keys.json"

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

# Per-model native output dimensionality (best quality = no MRL truncation)
_GEMINI_DIMS: dict[str, int] = {
    "gemini-embedding-001": 768,   # native 768
    "gemini-embedding-2": 3072,    # native 3072 — use full for best quality
}
_GEMINI_EMBED_DIM = 768  # fallback for unknown models


def _load_gemini_keys() -> list[str]:
    """Load Gemini API keys from the shared keys file."""
    if _GEMINI_KEYS_PATH.exists():
        try:
            data = json.loads(_GEMINI_KEYS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [k.strip() for k in data if isinstance(k, str) and k.strip()]
        except (json.JSONDecodeError, OSError):
            pass
    return []


class _GeminiEmbedder:
    """Lightweight Gemini embedding API client matching FastEmbed's .embed() interface."""

    def __init__(self, model_name: str):
        # Strip our prefix: "google/gemini-embedding-001" -> "gemini-embedding-001"
        self._api_model = model_name.split("/", 1)[-1]
        self._dims = _GEMINI_DIMS.get(self._api_model, _GEMINI_EMBED_DIM)
        self._keys = _load_gemini_keys()
        self._key_idx = 0
        if not self._keys:
            raise RuntimeError("No Gemini API keys configured. Add keys in Settings → Providers.")

    def _next_key(self) -> str:
        key = self._keys[self._key_idx % len(self._keys)]
        self._key_idx += 1
        return key

    def embed(self, texts: list[str], batch_size: int = 50, *, task_type: str = "RETRIEVAL_DOCUMENT"):
        """Yield embedding vectors one at a time (matches FastEmbed generator interface)."""
        import httpx

        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            vectors = self._embed_batch_httpx(batch, task_type)
            yield from vectors

    def _embed_batch_httpx(self, texts: list[str], task_type: str) -> list[list[float]]:
        """Call Gemini batchEmbedContents endpoint."""
        import httpx

        url = f"{_GEMINI_BASE}/models/{self._api_model}:batchEmbedContents"
        key = self._next_key()
        payload = {
            "requests": [
                {
                    "model": f"models/{self._api_model}",
                    "content": {"parts": [{"text": t}]},
                    "taskType": task_type,
                    "outputDimensionality": self._dims,
                }
                for t in texts
            ]
        }
        resp = httpx.post(
            url,
            json=payload,
            params={"key": key},
            timeout=60.0,
        )
        if resp.status_code != 200:
            logger.warning("Gemini embed API error %d: %s", resp.status_code, resp.text[:200])
            # Retry once with next key
            key = self._next_key()
            resp = httpx.post(url, json=payload, params={"key": key}, timeout=60.0)
            if resp.status_code != 200:
                raise RuntimeError(f"Gemini embed failed: {resp.status_code} {resp.text[:200]}")

        data = resp.json()
        embeddings = data.get("embeddings", [])
        if len(embeddings) != len(texts):
            raise RuntimeError(
                f"Gemini embed returned {len(embeddings)} vectors for {len(texts)} texts"
            )
        return [e["values"] for e in embeddings]
