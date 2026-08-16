"""Embedding vector cache (numpy .npz) for memory search."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger("sable.memory_search")

_CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "system"


def _cache_path_for(model_name: str, base_dir: Path | None = None) -> Path:
    """Per-model cache file so switching models doesn't invalidate other caches."""
    slug = model_name.replace("/", "_").replace(" ", "_")
    d = base_dir if base_dir else _CACHE_DIR
    return d / f"memory_cache_{slug}.npz"


def save_cache(
    model_name: str,
    normed_vectors: np.ndarray,
    entries: list[str],
    entry_meta: list[dict[str, str]],
    cache_dir: Path | None = None,
) -> None:
    """Persist vectors + metadata to .npz. Best-effort; never raises."""
    try:
        meta_json = json.dumps(entry_meta, ensure_ascii=False)
        np.savez_compressed(
            _cache_path_for(model_name, cache_dir),
            vectors=normed_vectors,
            entries=np.array(entries, dtype=object),
            meta=np.array(meta_json),
            model_name=np.array(model_name),
        )
    except Exception:
        pass  # cache is best-effort; never break search over it


def load_cache_data(model_name: str, cache_dir: Path | None = None) -> dict[str, Any] | None:
    """Load raw cache arrays for the given model. Returns None if missing/corrupt."""
    cache_path = _cache_path_for(model_name, cache_dir)
    if not cache_path.exists():
        return None
    try:
        data = np.load(cache_path, allow_pickle=True)
        return {
            "vectors": data["vectors"],
            "entries": list(data["entries"]),
            "meta": json.loads(str(data["meta"])),
        }
    except Exception:
        return None


def clear_cache_file(model_name: str, cache_dir: Path | None = None) -> None:
    """Delete the .npz cache file for a model (best-effort)."""
    try:
        cache_path = _cache_path_for(model_name, cache_dir)
        if cache_path.exists():
            cache_path.unlink()
    except OSError:
        pass
