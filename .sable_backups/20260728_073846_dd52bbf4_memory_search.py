"""Semantic memory search using FastEmbed models with hybrid vector+keyword scoring."""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

_BRAIN_DIR = Path(__file__).resolve().parent.parent / "Brain"
_MEMORY_PATH = _BRAIN_DIR / "Memory.json"
_PROTECTED_PATH = _BRAIN_DIR / "Protected.json"

# Empirically calibrated (2026-07-27) against the hybrid score
# (0.7*vector + 0.3*keyword) via calibrate_thresholds.py — each value sits
# between that model's false-positive ceiling and true-match floor.
# NOTE: thenlper/gte-base could NOT be calibrated — fastembed returns ragged
# embeddings for it (numpy "inhomogeneous shape" error on load), so it's
# currently unusable anyway; its value below is just the old guess.
# Calibrated 2026-07-28 against hybrid score (0.7v + 0.3k) using 50 real
# user prompts vs real Memory.json — balanced = midpoint(p25, median).
MODEL_THRESHOLDS: dict[str, float] = {
    "jinaai/jina-embeddings-v2-small-en": 0.596,
    "snowflake/snowflake-arctic-embed-xs": 0.546,
    "BAAI/bge-small-en-v1.5": 0.504,
}

DEFAULT_MODEL = "snowflake/snowflake-arctic-embed-xs"
DEFAULT_TOP_K = 10

VECTOR_WEIGHT = 0.7
KEYWORD_WEIGHT = 0.3
PROTECTED_BOOST = 0.15

_STOPWORDS = frozenset({
    "the","a","an","is","are","was","were","be","been","being",
    "have","has","had","do","does","did","will","would","could","should",
    "may","might","shall","can","need","to","of","in","for","on","with",
    "at","by","from","as","into","through","during","before","after",
    "above","below","between","out","off","over","under","again","then",
    "once","here","there","when","where","why","how","all","both","each",
    "few","more","most","other","some","such","no","nor","not","only",
    "own","same","so","than","too","very","just","because","but","and",
    "or","if","while","about","what","which","who","whom","this","that",
    "these","those","i","me","my","we","our","you","your","he","him",
    "his","she","her","it","its","they","them","their",
})


def _tokenize(text: str) -> set[str]:
    tokens = re.findall(r"[a-z0-9_]+", text.lower())
    return {t for t in tokens if t not in _STOPWORDS and len(t) > 1}


def _keyword_score(query_tokens: set[str], entry_tokens: set[str]) -> float:
    if not query_tokens:
        return 0.0
    return len(query_tokens & entry_tokens) / len(query_tokens)


def _is_expired(entry: dict) -> bool:
    expires = str(entry.get("expires_at") or "").strip()
    if not expires:
        return False
    try:
        return datetime.fromisoformat(expires) < datetime.now()
    except ValueError:
        return False


class MemorySearcher:
    _instance: MemorySearcher | None = None
    _instance_lock = threading.Lock()

    def __new__(cls) -> MemorySearcher:
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._model_name: str = DEFAULT_MODEL
        self._custom_thresholds: dict[str, float] = {}
        self._model: Any = None
        self._entries: list[str] = []
        self._normed_vectors: np.ndarray | None = None
        self._entry_meta: list[dict[str, str]] = []
        self._entry_tokens: list[set[str]] = []
        self._load_lock = threading.RLock()
        self._initialized = True

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def threshold(self) -> float:
        return self.get_threshold(self._model_name)

    def get_threshold(self, model_name: str) -> float:
        if model_name in self._custom_thresholds:
            return self._custom_thresholds[model_name]
        return MODEL_THRESHOLDS.get(model_name, 0.5)

    def set_model(self, model_name: str) -> None:
        with self._load_lock:
            if model_name != self._model_name:
                self._model_name = model_name
                self._model = None
                self._normed_vectors = None

    def set_thresholds(self, thresholds: dict[str, float]) -> None:
        with self._load_lock:
            self._custom_thresholds = {
                k: max(0.0, min(1.0, float(v))) for k, v in thresholds.items()
            }

    def get_custom_thresholds(self) -> dict[str, float]:
        return dict(self._custom_thresholds)

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        from fastembed import TextEmbedding

        self._model = TextEmbedding(model_name=self._model_name)

    def _ensure_loaded(self) -> None:
        with self._load_lock:
            if self._normed_vectors is not None:
                return
            self._ensure_model()
            self._load_memory_entries()
            if self._entries:
                vecs = np.array(list(self._model.embed(self._entries)), dtype="float32")
                norms = np.linalg.norm(vecs, axis=1, keepdims=True)
                safe_norms = np.where(norms == 0, 1.0, norms)
                self._normed_vectors = vecs / safe_norms
            else:
                self._normed_vectors = np.empty((0, 0), dtype="float32")

    def _add_entry(self, text: str, key: str, value: str, category: str) -> None:
        self._entries.append(text)
        self._entry_meta.append({"key": key, "value": value, "category": category})
        self._entry_tokens.append(_tokenize(text))

    def _load_memory_entries(self) -> None:
        self._entries = []
        self._entry_meta = []
        self._entry_tokens = []

        # Load main memory (semantic, episodic, procedural, ephemeral)
        if _MEMORY_PATH.exists():
            try:
                data = json.loads(_MEMORY_PATH.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            if isinstance(data, dict):
                for cat_key in ("semantic", "episodic", "procedural", "ephemeral"):
                    for e in data.get(cat_key, []):
                        if not isinstance(e, dict):
                            continue
                        if cat_key == "ephemeral" and _is_expired(e):
                            continue
                        k = str(e.get("key", "")).strip()
                        v = str(e.get("value", "")).strip()
                        if not v:
                            continue
                        text = f"{k}: {v}" if k else v
                        self._add_entry(text, k, v, cat_key)
            elif isinstance(data, list):
                for e in data:
                    if isinstance(e, dict):
                        k = str(e.get("key", e.get("topic", ""))).strip()
                        v = str(e.get("value", e.get("content", ""))).strip()
                        if not v:
                            v = str(e)
                        text = f"{k}: {v}" if k else v
                    else:
                        text = str(e)
                        k, v = "", text
                    self._add_entry(text, k, v, "uncategorized")

        # Load protected memory (always active, never expires)
        if _PROTECTED_PATH.exists():
            try:
                pdata = json.loads(_PROTECTED_PATH.read_text(encoding="utf-8"))
            except Exception:
                pdata = {}
            entries = pdata.get("protected", []) if isinstance(pdata, dict) else []
            for e in entries:
                if not isinstance(e, dict):
                    continue
                k = str(e.get("key", "")).strip()
                v = str(e.get("value", "")).strip()
                if not v:
                    continue
                text = f"{k}: {v}" if k else v
                self._add_entry(text, k, v, "protected")

    def reload_memory(self) -> None:
        with self._load_lock:
            self._normed_vectors = None
            self._entries = []
            self._entry_meta = []
            self._entry_tokens = []

    def search(
        self,
        query: str,
        top_k: int = DEFAULT_TOP_K,
        threshold: float | None = None,
    ) -> list[dict[str, Any]]:
        self._ensure_loaded()
        with self._load_lock:
            if not self._entries or self._normed_vectors is None or self._model is None:
                return []
            q_vec = np.array(list(self._model.embed([query]))[0], dtype="float32")
            q_norm_value = np.linalg.norm(q_vec)
            q_norm = q_vec if q_norm_value == 0 else q_vec / q_norm_value
            vector_scores = self._normed_vectors @ q_norm

            # Hybrid blend: vector similarity + keyword coverage
            query_tokens = _tokenize(query)
            keyword_scores = np.array(
                [_keyword_score(query_tokens, et) for et in self._entry_tokens],
                dtype="float32",
            )
            scores = VECTOR_WEIGHT * vector_scores + KEYWORD_WEIGHT * keyword_scores

            # Protected entries get a relevance boost
            for i, meta in enumerate(self._entry_meta):
                if meta["category"] == "protected":
                    scores[i] += PROTECTED_BOOST

            cutoff = threshold if threshold is not None else self.threshold
            ranked = np.argsort(-scores)
            results: list[dict[str, Any]] = []
            for idx in ranked[:top_k]:
                if scores[idx] < cutoff:
                    break
                meta = self._entry_meta[idx]
                results.append({
                    "key": meta["key"],
                    "value": meta["value"],
                    "category": meta["category"],
                    "score": float(scores[idx]),
                })
            return results

    def format_for_prompt(self, results: list[dict[str, Any]]) -> str:
        if not results:
            return ""
        lines = ["[RELEVANT MEMORY CONTEXT]"]
        for r in results:
            if r["key"]:
                lines.append(f"- **{r['key']}**: {r['value']}")
            else:
                lines.append(f"- {r['value']}")
        return "\n".join(lines)


def get_searcher() -> MemorySearcher:
    return MemorySearcher()


def list_available_models() -> list[dict[str, Any]]:
    return [
        {"id": name, "threshold": thresh}
        for name, thresh in MODEL_THRESHOLDS.items()
    ]
