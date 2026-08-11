"""Semantic memory search using FastEmbed models with hybrid vector+keyword scoring."""

from __future__ import annotations

import gc
import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger("sable.memory_search")

_BRAIN_DIR = Path(__file__).resolve().parent.parent / "Brain"
_MEMORY_PATH = _BRAIN_DIR / "Memory.json"
_PROTECTED_PATH = _BRAIN_DIR / "Protected.json"

_CACHE_DIR = Path(__file__).resolve().parent.parent / "system"
_GEMINI_KEYS_PATH = _CACHE_DIR / ".gemini_api_keys.json"

def _cache_path_for(model_name: str, base_dir: Path | None = None) -> Path:
    """Per-model cache file so switching models doesn't invalidate other caches."""
    slug = model_name.replace("/", "_").replace(" ", "_")
    d = base_dir if base_dir else _CACHE_DIR
    return d / f"memory_cache_{slug}.npz"

# Per-model thresholds for MultiRRF/MultiField scoring (calibrated 2026-08-11).
# Each model produces different score distributions, so thresholds are per-model.
# "proc" = procedural MultiRRF threshold (4-field, scores ~2.5–4.5)
# "std"  = standard MultiField threshold (2-field, scores ~1.0–2.2)
# NOTE: thenlper/gte-base could NOT be calibrated — fastembed returns ragged
# embeddings for it (numpy "inhomogeneous shape" error on load).
MODEL_THRESHOLDS: dict[str, dict[str, float]] = {
    "snowflake/snowflake-arctic-embed-xs": {"proc": 2.57, "std": 1.49},
    "jinaai/jina-embeddings-v2-small-en": {"proc": 2.57, "std": 1.55},
    "BAAI/bge-small-en-v1.5": {"proc": 2.20, "std": 1.29},
    "google/gemini-embedding-001": {"proc": 2.70, "std": 1.45},  # uncalibrated
}

DEFAULT_MODEL = "snowflake/snowflake-arctic-embed-xs"
DEFAULT_TOP_K = 10

# Legacy weights (kept for backward compat)
VECTOR_WEIGHT = 0.7
KEYWORD_WEIGHT = 0.3
PROTECTED_BOOST = 0.15
EPISODIC_DECAY_RATE = 0.95  # Score multiplier per day since last access

# ─── MultiRRF / MultiField scoring (calibrated 2026-08-11) ──────────────────
# Procedural: 4-field weighted RRF
PROC_FIELD_WEIGHTS: dict[str, float] = {"key": 2.0, "keyword": 1.8, "trigger": 1.5, "value": 0.8}
# Other types: 2-field weighted
STD_FIELD_WEIGHTS: dict[str, float] = {"key": 2.0, "value": 1.0}
# Hybrid blend per field
FIELD_VEC_WEIGHT = 0.6
FIELD_KW_WEIGHT = 0.4

# Calibrated thresholds (fallback defaults — per-model values in MODEL_THRESHOLDS)
PROC_THRESHOLD_MULTI = 2.57   # Procedural MultiRRF scores range ~2.5–4.0
STD_THRESHOLD_MULTI = 1.49    # Semantic/Episodic/Ephemeral MultiField range ~1.4–2.2

# Default allocation (overridable in settings)
DEFAULT_TOP_SKILL = 5     # max procedural results
DEFAULT_TOP_MEMORY = 4    # max semantic/episodic/ephemeral results
DEFAULT_TOP_TOTAL = 9     # total injected (proc gets ceil, others get floor)

# ─── Guard: reject garbage before embedding ──────────────────────────────────
_GARBAGE_WORDS = frozenset({
    "fuck", "shit", "ass", "bitch", "dick", "cock", "pussy", "cunt",
    "motherfucker", "bullshit", "asshole", "dumbass", "jackass",
    "penis", "vagina", "boobs", "tits", "nigger", "faggot",
    "retard", "whore", "slut", "bastard", "wanker", "twat",
    "cum", "semen", "orgasm", "masturbate", "porn",
    "kill", "yourself", "kys",
    "lol", "lmao", "rofl", "xd", "hehe", "haha",
    "asdf", "qwerty", "zxcv",
    "blah", "bluh", "bleh", "meh",
    "damn", "hell", "wtf", "stfu",
    "fucking", "fucked", "bull", "crap",
})

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


def _is_valid_query(query: str) -> bool:
    """Guard: reject garbage before any embedding compute. Zero-cost string checks."""
    stripped = query.strip()
    if not stripped or len(stripped) < 3:
        return False
    alpha_num = re.findall(r'[a-zA-Z0-9]', stripped)
    if len(alpha_num) < 3:
        return False
    tokens = _tokenize(stripped)
    if not tokens or len(tokens) < 2:
        return False
    garbage_count = sum(1 for t in tokens if t in _GARBAGE_WORDS)
    if garbage_count > 0 and garbage_count / len(tokens) > 0.5:
        return False
    return True


def _is_expired(entry: dict) -> bool:
    expires = str(entry.get("expires_at") or "").strip()
    if not expires:
        return False
    try:
        dt = datetime.fromisoformat(expires)
        # Normalize: strip tzinfo to compare naive-to-naive
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt < datetime.now()
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Gemini API embedding backend
# ---------------------------------------------------------------------------

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

    def __init__(self, memory_path: Path | None = None, protected_path: Path | None = None, cache_dir: Path | None = None) -> None:
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
        self._memory_path = memory_path or _MEMORY_PATH
        self._protected_path = protected_path or _PROTECTED_PATH
        self._cache_dir = cache_dir
        # Procedural MultiRRF storage (separate from main vector index)
        self._proc_entries: list[dict] = []  # raw procedural entries with keyword/trigger
        self._proc_field_vecs: dict[str, np.ndarray] = {}  # field -> normalized vectors
        self._proc_field_tokens: dict[str, list[set[str]]] = {}  # field -> tokenized
        self._proc_index_built = False
        self._initialized = True

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def threshold(self) -> float:
        """Legacy compat — returns std threshold."""
        return self.get_std_threshold(self._model_name)

    def get_proc_threshold(self, model_name: str | None = None) -> float:
        """Procedural MultiRRF threshold for the given model."""
        model = model_name or self._model_name
        if model in self._custom_thresholds and isinstance(self._custom_thresholds[model], dict):
            return self._custom_thresholds[model].get("proc", PROC_THRESHOLD_MULTI)
        return MODEL_THRESHOLDS.get(model, {}).get("proc", PROC_THRESHOLD_MULTI)

    def get_std_threshold(self, model_name: str | None = None) -> float:
        """Standard MultiField threshold for the given model."""
        model = model_name or self._model_name
        if model in self._custom_thresholds and isinstance(self._custom_thresholds[model], dict):
            return self._custom_thresholds[model].get("std", STD_THRESHOLD_MULTI)
        return MODEL_THRESHOLDS.get(model, {}).get("std", STD_THRESHOLD_MULTI)

    def set_model(self, model_name: str) -> None:
        with self._load_lock:
            if model_name != self._model_name:
                self._model_name = model_name
                self._model = None
                self._normed_vectors = None
                self._proc_index_built = False

    def set_thresholds(self, thresholds: dict[str, Any]) -> None:
        """
        Accepts either:
          - New format: {"model_name": {"proc": X, "std": Y}}
          - Legacy flat: {"model_name": 0.5} (treated as std only)
        """
        with self._load_lock:
            result: dict[str, Any] = {}
            for model, val in thresholds.items():
                if isinstance(val, dict):
                    result[str(model)] = {
                        "proc": float(val.get("proc", PROC_THRESHOLD_MULTI)),
                        "std": float(val.get("std", STD_THRESHOLD_MULTI)),
                    }
                else:
                    # Legacy: single float → treat as std threshold
                    result[str(model)] = {
                        "proc": PROC_THRESHOLD_MULTI,
                        "std": float(val),
                    }
            self._custom_thresholds = result

    def get_custom_thresholds(self) -> dict[str, Any]:
        return dict(self._custom_thresholds)

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        if self._model_name.startswith("google/"):
            self._model = _GeminiEmbedder(self._model_name)
        else:
            from fastembed import TextEmbedding
            self._model = TextEmbedding(model_name=self._model_name, enable_cpu_mem_arena=False)

    def _embed_texts(self, texts: list[str], *, is_query: bool = False) -> np.ndarray:
        """Embed texts, handling both FastEmbed and Gemini API models."""
        if isinstance(self._model, _GeminiEmbedder):
            task = "RETRIEVAL_QUERY" if is_query else "RETRIEVAL_DOCUMENT"
            vecs = np.array(list(self._model.embed(texts, task_type=task)), dtype="float32")
        else:
            vecs = np.array(list(self._model.embed(texts, batch_size=32)), dtype="float32")
        # Normalize
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        return vecs / np.where(norms == 0, 1.0, norms)

    def _save_cache(self) -> None:
        try:
            meta_json = json.dumps(self._entry_meta, ensure_ascii=False)
            np.savez_compressed(
                _cache_path_for(self._model_name, self._cache_dir),
                vectors=self._normed_vectors,
                entries=np.array(self._entries, dtype=object),
                meta=np.array(meta_json),
                model_name=np.array(self._model_name),
            )
        except Exception:
            pass  # cache is best-effort; never break search over it

    def _load_cache_data(self) -> dict[str, Any] | None:
        """Load raw cache arrays for the current model. Returns None if missing."""
        cache_path = _cache_path_for(self._model_name, self._cache_dir)
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

    def _populate_from_cache(self, cached: dict[str, Any]) -> None:
        self._normed_vectors = cached["vectors"]
        self._entries = cached["entries"]
        self._entry_meta = cached["meta"]
        self._entry_tokens = [_tokenize(e) for e in self._entries]

    def _ensure_loaded(self) -> None:
        with self._load_lock:
            if self._normed_vectors is not None:
                return
            self._ensure_model()

            # Always parse current entries from JSON (cheap)
            self._load_memory_entries()
            if not self._entries:
                self._normed_vectors = np.empty((0, 0), dtype="float32")
                return

            cached = self._load_cache_data()
            if cached is not None:
                # Build lookup: entry_text -> cached vector index
                cache_index: dict[str, int] = {
                    text: i for i, text in enumerate(cached["entries"])
                }
                # Check if current entries exactly match cache (fast path)
                if cached["entries"] == self._entries:
                    self._populate_from_cache(cached)
                    return

                # Incremental: reuse cached vectors, embed only new entries
                new_indices: list[int] = []
                reuse_vectors: list[np.ndarray | None] = [None] * len(self._entries)
                for i, text in enumerate(self._entries):
                    if text in cache_index:
                        reuse_vectors[i] = cached["vectors"][cache_index[text]]
                    else:
                        new_indices.append(i)

                if new_indices:
                    new_texts = [self._entries[i] for i in new_indices]
                    new_vecs = self._embed_texts(new_texts)
                    for j, idx in enumerate(new_indices):
                        reuse_vectors[idx] = new_vecs[j]

                self._normed_vectors = np.vstack(reuse_vectors)
                self._entry_tokens = [_tokenize(e) for e in self._entries]
            else:
                # No cache at all — full embed
                self._normed_vectors = self._embed_texts(self._entries)
                self._entry_tokens = [_tokenize(e) for e in self._entries]

            self._save_cache()

    def _add_entry(self, text: str, key: str, value: str, category: str, extra_meta: dict | None = None) -> None:
        self._entries.append(text)
        meta: dict[str, Any] = {"key": key, "value": value, "category": category}
        if extra_meta:
            meta.update(extra_meta)
        self._entry_meta.append(meta)
        self._entry_tokens.append(_tokenize(text))

    def _load_memory_entries(self) -> None:
        self._entries = []
        self._entry_meta = []
        self._entry_tokens = []
        self._proc_entries = []  # Reset procedural

        # Load main memory (semantic, episodic, procedural, ephemeral)
        # (full vectorize path — cache miss or stale)
        if self._memory_path.exists():
            try:
                data = json.loads(self._memory_path.read_text(encoding="utf-8"))
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
                        # Store procedural entries separately with enriched fields
                        if cat_key == "procedural":
                            self._proc_entries.append({
                                "key": k,
                                "value": v,
                                "keyword": str(e.get("keyword", "")).strip(),
                                "trigger": str(e.get("trigger", "")).strip(),
                            })
                        text = f"{k}: {v}" if k else v
                        extra = None
                        if cat_key == "episodic" and e.get("last_accessed"):
                            extra = {"last_accessed": str(e["last_accessed"])}
                        self._add_entry(text, k, v, cat_key, extra)
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
        if self._protected_path.exists():
            try:
                pdata = json.loads(self._protected_path.read_text(encoding="utf-8"))
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

    def _build_procedural_index(self) -> None:
        """Build 4-field vector index for procedural entries (MultiRRF)."""
        if self._proc_index_built or not self._proc_entries:
            return
        self._proc_field_vecs = {}
        self._proc_field_tokens = {}
        for field in PROC_FIELD_WEIGHTS:
            texts = [e.get(field, "") or e.get("value", "") for e in self._proc_entries]
            vecs = self._embed_texts(texts)
            self._proc_field_vecs[field] = vecs
            self._proc_field_tokens[field] = [_tokenize(t) for t in texts]
        self._proc_index_built = True

    def _score_procedural(self, q_vec: np.ndarray, q_tokens: set[str]) -> list[dict]:
        """MultiRRF scoring for procedural entries. Returns sorted results."""
        if not self._proc_entries:
            return []
        n = len(self._proc_entries)
        final_scores = np.zeros(n, dtype="float32")
        for field, weight in PROC_FIELD_WEIGHTS.items():
            vec_scores = self._proc_field_vecs[field] @ q_vec
            if len(q_tokens) >= 2:
                kw_scores = np.array(
                    [_keyword_score(q_tokens, et) for et in self._proc_field_tokens[field]],
                    dtype="float32",
                )
            else:
                kw_scores = np.zeros(n, dtype="float32")
            hybrid = FIELD_VEC_WEIGHT * vec_scores + FIELD_KW_WEIGHT * kw_scores
            final_scores += weight * hybrid

        cutoff = self.get_proc_threshold()
        ranked = np.argsort(-final_scores)
        results = []
        for idx in ranked:
            if final_scores[idx] < cutoff:
                break
            results.append({
                "key": self._proc_entries[idx]["key"],
                "value": self._proc_entries[idx]["value"],
                "category": "procedural",
                "score": float(final_scores[idx]),
            })
        return results

    def _score_standard(self, q_vec: np.ndarray, q_tokens: set[str], allowed_categories: set[str] | None) -> list[dict]:
        """MultiField (2-field) scoring for semantic/episodic/ephemeral/protected."""
        if not self._entries or self._normed_vectors is None:
            return []
        # 2-field scoring: key and value separately
        n = len(self._entries)
        # Key scores: embed just the keys
        key_texts = [m["key"] for m in self._entry_meta]
        # Value scores: use the existing combined vectors as proxy for value
        # (full re-embed of keys is expensive; use combined vector for value, keyword overlap for key)
        vec_scores = self._normed_vectors @ q_vec

        # Keyword scoring per entry
        if len(q_tokens) >= 3:
            kw_scores = np.array(
                [_keyword_score(q_tokens, et) for et in self._entry_tokens],
                dtype="float32",
            )
        else:
            kw_scores = np.zeros(n, dtype="float32")

        # Weighted: key gets higher weight via keyword, value via vector
        scores = STD_FIELD_WEIGHTS["key"] * (FIELD_VEC_WEIGHT * vec_scores + FIELD_KW_WEIGHT * kw_scores) + \
                 STD_FIELD_WEIGHTS["value"] * vec_scores

        # Apply episodic decay and protected boost
        now = datetime.now()
        for i, meta in enumerate(self._entry_meta):
            if meta["category"] == "protected":
                scores[i] += PROTECTED_BOOST
            elif meta["category"] == "episodic" and "last_accessed" in meta:
                try:
                    last = datetime.fromisoformat(meta["last_accessed"])
                    days_since = max(0, (now - last).total_seconds() / 86400)
                    scores[i] *= EPISODIC_DECAY_RATE ** days_since
                except (ValueError, TypeError):
                    pass

        cutoff = self.get_std_threshold()
        ranked = np.argsort(-scores)
        results = []
        accessed_episodic_keys: list[str] = []
        for idx in ranked:
            if scores[idx] < cutoff:
                break
            meta = self._entry_meta[idx]
            # Skip procedural (handled separately) and filter categories
            if meta["category"] == "procedural":
                continue
            if allowed_categories is not None and meta["category"] not in allowed_categories:
                continue
            results.append({
                "key": meta["key"],
                "value": meta["value"],
                "category": meta["category"],
                "score": float(scores[idx]),
            })
            if meta["category"] == "episodic":
                accessed_episodic_keys.append(meta["key"])

        # Update episodic access times
        if accessed_episodic_keys:
            now_str = now.isoformat()
            for meta in self._entry_meta:
                if meta["category"] == "episodic" and meta["key"] in accessed_episodic_keys:
                    meta["last_accessed"] = now_str
            self._persist_episodic_access(accessed_episodic_keys, now_str)

        return results

    def _persist_episodic_access(self, keys: list[str], timestamp: str) -> None:
        """Write updated last_accessed timestamps back to Memory.json for episodic entries."""
        if not self._memory_path.exists():
            return
        try:
            data = json.loads(self._memory_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            key_set = set(keys)
            changed = False
            for e in data.get("episodic", []):
                if isinstance(e, dict) and e.get("key") in key_set:
                    e["last_accessed"] = timestamp
                    changed = True
            if changed:
                self._memory_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def clear_cache(self) -> None:
        """Delete the current model's .npz cache file and force a full re-embed on next search."""
        with self._load_lock:
            try:
                cache_path = _cache_path_for(self._model_name)
                if cache_path.exists():
                    cache_path.unlink()
            except OSError:
                pass
            self._normed_vectors = None
            self._entries = []
            self._entry_meta = []
            self._entry_tokens = []
            gc.collect()

    def rebuild_cache(self) -> int:
        """Clear cache and immediately re-embed all entries. Returns entry count."""
        self.clear_cache()
        self._ensure_loaded()
        return len(self._entries)

    def reload_memory(self) -> None:
        with self._load_lock:
            self._normed_vectors = None
            self._entries = []
            self._entry_meta = []
            self._entry_tokens = []
            self._proc_entries = []
            self._proc_field_vecs = {}
            self._proc_field_tokens = {}
            self._proc_index_built = False
            gc.collect()

    def search(
        self,
        query: str,
        top_k: int = DEFAULT_TOP_K,
        threshold: float | None = None,
        allowed_categories: set[str] | None = None,
        top_skill: int | None = None,
        top_memory: int | None = None,
        top_total: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Search memory with MultiRRF (procedural) + MultiField (others).
        Procedural gets priority allocation: ceil(top_total/2) slots, others get floor.
        """
        # Guard: reject garbage before any embedding
        if not _is_valid_query(query):
            return []

        self._ensure_loaded()
        with self._load_lock:
            if not self._entries or self._normed_vectors is None or self._model is None:
                return []

            # Resolve allocation
            _top_skill = top_skill if top_skill is not None else DEFAULT_TOP_SKILL
            _top_memory = top_memory if top_memory is not None else DEFAULT_TOP_MEMORY
            _top_total = top_total if top_total is not None else DEFAULT_TOP_TOTAL

            # Embed query once (shared across both scorers)
            q_vec = self._embed_texts([query], is_query=True)[0]
            q_tokens = _tokenize(query)

            # Build procedural index if needed
            if self._proc_entries and not self._proc_index_built:
                self._build_procedural_index()

            # Score procedural (MultiRRF, 4-field)
            proc_results = self._score_procedural(q_vec, q_tokens)

            # Score standard (MultiField, 2-field) — excludes procedural
            std_results = self._score_standard(q_vec, q_tokens, allowed_categories)

            # Priority allocation: procedural gets ceil(top_total/2), others get floor
            proc_slots = min(_top_skill, -(-_top_total // 2))  # ceil division
            mem_slots = min(_top_memory, _top_total // 2)

            # Fill procedural first (priority)
            final_proc = proc_results[:proc_slots]
            # Fill others with remaining budget
            remaining = _top_total - len(final_proc)
            final_mem = std_results[:min(mem_slots, remaining)]

            # Merge: procedural first, then others
            results = final_proc + final_mem
            return results[:_top_total]

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


_project_searchers: dict[str, MemorySearcher] = {}

def get_project_searcher(project_id: str) -> MemorySearcher:
    """Get or create a vector searcher scoped to a project's memory directory."""
    if project_id in _project_searchers:
        return _project_searchers[project_id]
    proj_dir = Path(__file__).resolve().parent.parent / "system" / "projects" / project_id
    proj_dir.mkdir(parents=True, exist_ok=True)
    mem_path = proj_dir / "Memory.json"
    prot_path = proj_dir / "Protected.json"
    # Bypass singleton __new__ to create independent instance
    searcher = object.__new__(MemorySearcher)
    searcher._initialized = False
    searcher.__init__(memory_path=mem_path, protected_path=prot_path, cache_dir=proj_dir)
    _project_searchers[project_id] = searcher
    return searcher


def reload_project_searcher(project_id: str) -> None:
    """Force a project searcher to reload its entries from disk."""
    if project_id in _project_searchers:
        s = _project_searchers[project_id]
        with s._load_lock:
            s._normed_vectors = None
            s._entries = []


def list_available_models() -> list[dict[str, Any]]:
    return [
        {"id": name, "proc_threshold": t["proc"], "std_threshold": t["std"]}
        for name, t in MODEL_THRESHOLDS.items()
    ]
