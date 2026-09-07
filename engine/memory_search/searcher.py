"""MemorySearcher — hybrid vector+keyword semantic search over Brain memory files."""

from __future__ import annotations

import gc
import json
import logging
import math
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from .tokenize import _tokenize, _strip_paths, _is_expired
from .scoring import (
    MODEL_THRESHOLDS, DEFAULT_MODEL, DEFAULT_TOP_K,
    VECTOR_WEIGHT, KEYWORD_WEIGHT, KEY_TOKEN_WEIGHT,
    TRIGGER_IDF_WEIGHT, SOURCE_QUERY_WEIGHT, PROTECTED_BOOST,
    VECTOR_ONLY_MIN,
)
from .cache import _cache_path_for, save_cache, load_cache_data, clear_cache_file
from .embedder import _GeminiEmbedder

logger = logging.getLogger("sable.memory_search")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_BRAIN_DIR = _PROJECT_ROOT / "Brain"
_MEMORY_PATH = _BRAIN_DIR / "Memory.json"
_FASTEMBED_CACHE_DIR = _PROJECT_ROOT / "system" / "fastembed_cache"
_PROTECTED_PATH = _BRAIN_DIR / "Protected.json"
_PROCEDURAL_PATH = _BRAIN_DIR / "Procedural.json"


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
        self._trigger_tokens: list[set[str]] = []
        self._source_query_tokens: list[set[str]] = []
        self._idf_table: dict[str, float] = {}
        self._load_lock = threading.RLock()
        self._memory_path = memory_path or _MEMORY_PATH
        self._protected_path = protected_path or _PROTECTED_PATH
        self._cache_dir = cache_dir
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
        if self._model_name.startswith("google/"):
            self._model = _GeminiEmbedder(self._model_name)
        else:
            from fastembed import TextEmbedding
            self._model = TextEmbedding(
                model_name=self._model_name,
                cache_dir=str(_FASTEMBED_CACHE_DIR),
                enable_cpu_mem_arena=False,
            )

    def _embed_texts(self, texts: list[str], *, is_query: bool = False) -> np.ndarray:
        """Embed texts, handling both FastEmbed and Gemini API models."""
        if isinstance(self._model, _GeminiEmbedder):
            task = "RETRIEVAL_QUERY" if is_query else "RETRIEVAL_DOCUMENT"
            vecs = np.array(list(self._model.embed(texts, task_type=task)), dtype="float32")
        else:
            vecs = np.array(list(self._model.embed(texts, batch_size=32)), dtype="float32")
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        return vecs / np.where(norms == 0, 1.0, norms)

    def _save_cache(self) -> None:
        save_cache(self._model_name, self._normed_vectors, self._entries, self._entry_meta, self._cache_dir)

    def _load_cache_data(self) -> dict[str, Any] | None:
        return load_cache_data(self._model_name, self._cache_dir)

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

            self._load_memory_entries()
            if not self._entries:
                self._normed_vectors = np.empty((0, 0), dtype="float32")
                return

            cached = self._load_cache_data()
            if cached is not None:
                cache_index: dict[str, int] = {
                    text: i for i, text in enumerate(cached["entries"])
                }
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

    def _extract_retrieval_tokens(self, entry: dict) -> tuple[set[str], set[str]]:
        """Extract trigger/tag tokens and source_query tokens from a memory entry."""
        trigger_tokens: set[str] = set()
        for t in entry.get("triggers", []):
            trigger_tokens.update(_tokenize(str(t)))
        for t in entry.get("tags", []):
            trigger_tokens.update(_tokenize(str(t)))
        key = str(entry.get("key", ""))
        if key:
            trigger_tokens.update(_tokenize(key))

        sq_tokens: set[str] = set()
        sq = entry.get("source_query", "")
        if sq:
            sq_tokens = _tokenize(str(sq))

        return trigger_tokens, sq_tokens

    def _build_idf_table(self) -> None:
        """Build IDF weights from trigger/tag token document frequencies."""
        n = len(self._trigger_tokens)
        if n == 0:
            self._idf_table = {}
            return
        doc_freq: Counter = Counter()
        for tokens in self._trigger_tokens:
            for tok in tokens:
                doc_freq[tok] += 1
        self._idf_table = {
            tok: math.log(n / df) for tok, df in doc_freq.items()
        }

    def _load_memory_entries(self) -> None:
        self._entries = []
        self._entry_meta = []
        self._entry_tokens = []
        self._trigger_tokens = []
        self._source_query_tokens = []

        # Load main memory (semantic, episodic, ephemeral only)
        if self._memory_path.exists():
            try:
                data = json.loads(self._memory_path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            if isinstance(data, dict):
                for cat_key in ("semantic", "episodic", "ephemeral"):
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
                        embed_parts = [text]
                        if e.get("tags"):
                            embed_parts.append(" ".join(str(t) for t in e["tags"]))
                        if e.get("triggers"):
                            embed_parts.append(" ".join(str(t) for t in e["triggers"]))
                        if e.get("source_query"):
                            embed_parts.append(str(e["source_query"]))
                        embed_text = " ".join(embed_parts)
                        extra = None
                        if cat_key == "episodic" and e.get("last_accessed"):
                            extra = {"last_accessed": str(e["last_accessed"])}
                        self._add_entry(embed_text, k, v, cat_key, extra)
                        trig_toks, sq_toks = self._extract_retrieval_tokens(e)
                        self._trigger_tokens.append(trig_toks)
                        self._source_query_tokens.append(sq_toks)
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
                    self._trigger_tokens.append(set())
                    self._source_query_tokens.append(set())

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
                trig_toks, sq_toks = self._extract_retrieval_tokens(e)
                self._trigger_tokens.append(trig_toks)
                self._source_query_tokens.append(sq_toks)

        # Load procedural memory from separate file
        if _PROCEDURAL_PATH.exists():
            try:
                proc_data = json.loads(_PROCEDURAL_PATH.read_text(encoding="utf-8"))
            except Exception:
                proc_data = {}
            proc_entries = proc_data.get("procedural", []) if isinstance(proc_data, dict) else []
            for e in proc_entries:
                if not isinstance(e, dict):
                    continue
                k = str(e.get("key", "")).strip()
                v = str(e.get("value", "")).strip()
                if not v:
                    continue
                text = f"{k}: {v}" if k else v
                trigger = str(e.get("trigger", "")).strip()
                keywords = e.get("keywords", [])
                if trigger:
                    text += f" | trigger: {trigger}"
                if isinstance(keywords, list) and keywords:
                    text += f" | keywords: {' '.join(str(kw) for kw in keywords)}"
                proc_extra: dict[str, Any] = {}
                if trigger:
                    proc_extra["trigger"] = trigger
                if isinstance(keywords, list) and keywords:
                    proc_extra["keywords"] = [str(kw) for kw in keywords]
                self._add_entry(text, k, v, "procedural", proc_extra if proc_extra else None)
                trig_toks, sq_toks = self._extract_retrieval_tokens(e)
                if trigger:
                    trig_toks.update(_tokenize(trigger))
                if isinstance(keywords, list):
                    for kw in keywords:
                        trig_toks.update(_tokenize(str(kw)))
                self._trigger_tokens.append(trig_toks)
                self._source_query_tokens.append(sq_toks)

        self._build_idf_table()

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
            clear_cache_file(self._model_name, self._cache_dir)
            self._normed_vectors = None
            self._entries = []
            self._entry_meta = []
            self._entry_tokens = []
            self._trigger_tokens = []
            self._source_query_tokens = []
            self._idf_table = {}
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
            self._trigger_tokens = []
            self._source_query_tokens = []
            self._idf_table = {}
            gc.collect()

    # ── Scoring signals ──────────────────────────────────────────────────────

    def _idf_trigger_scores(self, query_tokens: set[str]) -> np.ndarray:
        """IDF-weighted trigger/tag matching. Returns per-entry scores."""
        n = len(self._entries)
        scores = np.zeros(n, dtype="float32")
        if len(query_tokens) < 1 or not self._idf_table:
            return scores
        min_overlap = 1 if len(query_tokens) <= 2 else 2
        max_possible = sum(self._idf_table.get(t, 1.0) for t in query_tokens)
        if max_possible == 0:
            return scores
        for i, trig_toks in enumerate(self._trigger_tokens):
            overlap = query_tokens & trig_toks
            if len(overlap) >= min_overlap:
                scores[i] = sum(self._idf_table.get(t, 1.0) for t in overlap) / max_possible
        return scores

    def _idf_source_query_scores(self, query_tokens: set[str]) -> np.ndarray:
        """IDF-weighted source_query matching. Returns per-entry scores."""
        n = len(self._entries)
        scores = np.zeros(n, dtype="float32")
        if len(query_tokens) < 1 or not self._idf_table:
            return scores
        max_possible = sum(self._idf_table.get(t, 1.0) for t in query_tokens)
        if max_possible == 0:
            return scores
        for i, sq_toks in enumerate(self._source_query_tokens):
            overlap = query_tokens & sq_toks
            if overlap:
                scores[i] = sum(self._idf_table.get(t, 1.0) for t in overlap) / max_possible
        return scores

    def _key_token_scores(self, query_tokens: set[str], query_lower: str) -> np.ndarray:
        """Key-token overlap scoring with substring bonus."""
        n = len(self._entries)
        scores = np.zeros(n, dtype="float32")
        if not query_tokens:
            return scores
        min_overlap = 1 if len(query_tokens) <= 2 else 2
        for i, meta in enumerate(self._entry_meta):
            key = meta.get("key", "")
            if not key:
                continue
            key_toks = _tokenize(key)
            overlap = query_tokens & key_toks
            if len(overlap) >= min_overlap:
                scores[i] = 25.0 * len(overlap) / max(len(key_toks), 1)
                if key.lower() in query_lower:
                    scores[i] += 20.0
        mx = scores.max()
        if mx > 0:
            scores /= mx
        return scores

    # ── Main search ──────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        top_k: int = DEFAULT_TOP_K,
        threshold: float | None = None,
        allowed_categories: set[str] | None = None,
        top_memory: int | None = None,
        top_procedural: int | None = None,
        top_total: int | None = None,
    ) -> list[dict[str, Any]]:
        """Search memories using 5-signal hybrid fusion (no per-query normalization)."""
        self._ensure_loaded()
        with self._load_lock:
            if not self._entries or self._normed_vectors is None or self._model is None:
                return []

            query_clean = _strip_paths(query)
            query_tokens = _tokenize(query_clean)
            if not query_tokens:
                return []
            query_lower = query_clean.lower()
            n = len(self._entries)

            # Signal 1: Vector cosine (0-1)
            q_vec = self._embed_texts([query], is_query=True)[0]
            vector_scores = self._normed_vectors @ q_vec

            # Signal 2: IDF-weighted trigger/tag match (0-1)
            trigger_scores = self._idf_trigger_scores(query_tokens)

            # Signal 3: IDF-weighted source_query match (0-1)
            sq_scores = self._idf_source_query_scores(query_tokens)

            # Signal 4: Key-token overlap (0-1)
            key_scores = self._key_token_scores(query_tokens, query_lower)

            # Signal 5: IDF-weighted keyword overlap (0-1), adaptive minimum
            keyword_scores = np.zeros(n, dtype="float32")
            if query_tokens and self._idf_table:
                q_idf_sum = sum(self._idf_table.get(t, 1.0) for t in query_tokens)
                kw_min_overlap = 1 if len(query_tokens) <= 2 else 2
                if q_idf_sum > 0:
                    for i, et in enumerate(self._entry_tokens):
                        overlap = query_tokens & et
                        if len(overlap) >= kw_min_overlap:
                            keyword_scores[i] = sum(self._idf_table.get(t, 1.0) for t in overlap) / q_idf_sum

            # Weighted fusion
            scores = (
                TRIGGER_IDF_WEIGHT * trigger_scores
                + SOURCE_QUERY_WEIGHT * sq_scores
                + VECTOR_WEIGHT * vector_scores
                + KEY_TOKEN_WEIGHT * key_scores
                + KEYWORD_WEIGHT * keyword_scores
            )

            # Protected boost
            for i, meta in enumerate(self._entry_meta):
                if meta["category"] == "protected":
                    has_text_signal = (trigger_scores[i] > 0 or keyword_scores[i] > 0)
                    if has_text_signal:
                        scores[i] += PROTECTED_BOOST

            # Vector-only gate
            for i in range(n):
                has_any_text = (trigger_scores[i] > 0 or keyword_scores[i] > 0)
                if not has_any_text and vector_scores[i] < VECTOR_ONLY_MIN:
                    scores[i] = 0.0

            cutoff = threshold if threshold is not None else self.threshold
            n_tokens = len(query_tokens)
            if n_tokens <= 1:
                cutoff = max(cutoff, 0.60)
            elif n_tokens <= 2:
                cutoff = max(cutoff, 0.40)
            ranked = np.argsort(-scores)

            # Split-budget selection
            use_split = top_total is not None
            if use_split:
                _tm = top_memory if top_memory is not None else 5
                _tp = top_procedural if top_procedural is not None else 3
                _tt = top_total
                proc_budget = min(_tp, _tt)
                other_budget = _tt - proc_budget
                other_budget = min(other_budget, _tm)
            else:
                proc_budget = top_k
                other_budget = top_k
                _tt = top_k

            results: list[dict[str, Any]] = []
            accessed_episodic_keys: list[str] = []
            proc_count = 0
            other_count = 0
            scan_limit = max(proc_budget, other_budget) * 4

            for idx in ranked[:scan_limit]:
                if scores[idx] < cutoff:
                    break
                meta = self._entry_meta[idx]
                if allowed_categories is not None and meta["category"] not in allowed_categories:
                    continue
                is_proc = meta["category"] == "procedural"
                if is_proc:
                    if proc_count >= proc_budget:
                        continue
                    proc_count += 1
                else:
                    if other_count >= other_budget:
                        continue
                    other_count += 1
                results.append({
                    "key": meta["key"],
                    "value": meta["value"],
                    "category": meta["category"],
                    "score": float(scores[idx]),
                })
                if meta["category"] == "episodic":
                    accessed_episodic_keys.append(meta["key"])
                if proc_count >= proc_budget and other_count >= other_budget:
                    break

            # Update last_accessed for retrieved episodic entries
            if accessed_episodic_keys:
                now_str = datetime.now().isoformat()
                for meta in self._entry_meta:
                    if meta["category"] == "episodic" and meta["key"] in accessed_episodic_keys:
                        meta["last_accessed"] = now_str
                self._persist_episodic_access(accessed_episodic_keys, now_str)

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


# ── Module-level accessors ───────────────────────────────────────────────────

def get_searcher() -> MemorySearcher:
    return MemorySearcher()


_project_searchers: dict[str, MemorySearcher] = {}


def get_project_searcher(project_id: str) -> MemorySearcher:
    """Get or create a vector searcher scoped to a project's memory directory."""
    if project_id in _project_searchers:
        return _project_searchers[project_id]
    proj_dir = Path(__file__).resolve().parent.parent.parent / "system" / "projects" / project_id
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
        {"id": name, "threshold": thresh}
        for name, thresh in MODEL_THRESHOLDS.items()
    ]
