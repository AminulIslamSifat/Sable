"""Semantic memory search using FastEmbed models with hybrid vector+keyword scoring."""

from __future__ import annotations

import gc
import json
import logging
import math
import re
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger("sable.memory_search")

_BRAIN_DIR = Path(__file__).resolve().parent.parent / "Brain"
_MEMORY_PATH = _BRAIN_DIR / "Memory.json"
_PROTECTED_PATH = _BRAIN_DIR / "Protected.json"
_PROCEDURAL_PATH = _BRAIN_DIR / "Procedural.json"

_CACHE_DIR = Path(__file__).resolve().parent.parent / "system"
_GEMINI_KEYS_PATH = _CACHE_DIR / ".gemini_api_keys.json"

def _cache_path_for(model_name: str, base_dir: Path | None = None) -> Path:
    """Per-model cache file so switching models doesn't invalidate other caches."""
    slug = model_name.replace("/", "_").replace(" ", "_")
    d = base_dir if base_dir else _CACHE_DIR
    return d / f"memory_cache_{slug}.npz"

# Calibrated 2026-08-14 for IDF-weighted hybrid fusion
# Threshold: garbage ceiling ~0.26, true-match floor ~0.40
MODEL_THRESHOLDS: dict[str, float] = {
    "jinaai/jina-embeddings-v2-small-en": 0.30,
    "snowflake/snowflake-arctic-embed-xs": 0.30,
    "BAAI/bge-small-en-v1.5": 0.30,
    "google/gemini-embedding-001": 0.30,
}

DEFAULT_MODEL = "snowflake/snowflake-arctic-embed-xs"
DEFAULT_TOP_K = 5

# Hybrid fusion weights (2026-08-14 benchmark calibrated)
VECTOR_WEIGHT = 0.25
KEYWORD_WEIGHT = 0.05
KEY_TOKEN_WEIGHT = 0.05
TRIGGER_IDF_WEIGHT = 0.40
SOURCE_QUERY_WEIGHT = 0.20
PROTECTED_BOOST = 0.15
EPISODIC_DECAY_RATE = 0.95  # Score multiplier per day since last access

_STOPWORDS = frozenset({
    "the","a","an","is","are","was","were","be","been","being",
    "have","has","had","do","does","did","will","would","could","should",
    "may","might","shall","can","need","dare","to","of","in","for","on","with",
    "at","by","from","as","into","through","during","before","after",
    "above","below","between","out","off","over","under","again","then",
    "once","here","there","when","where","why","how","all","both","each",
    "few","more","most","other","some","such","no","nor","not","only",
    "own","same","so","than","too","very","just","because","but","and",
    "or","if","while","about","what","which","who","whom","this","that",
    "these","those","i","me","my","we","our","you","your","he","him",
    "his","she","her","it","its","they","them","their",
    "up","down","also","now","any","get","got","make","take","see",
    "know","want","let","say","go","come","think","give","use","find",
    "tell","ask","work","seem","feel","try","leave","call","keep",
    "look","looks","looking","looked",
    "put","mean","become","show","run","move","like","thing","way",
    "back","still","new","one","two","first","last","long","great",
    "little","old","right","big","high","small","large","next",
    "early","young","important","public","bad","good","well","done",
    # Casual insults / filler — high frequency in user prompts, zero retrieval value
    "fuck","fucking","fucked","shit","shitty","damn","dumbass","idiot","idiotic",
    "moron","stupid","dumb","wtf","hell","crap","piss","ass","bullshit",
    "motherfucker","motherfucking","dumbfuck","dipshit","asshole","bitch",
    "ok","okay","hey","hi","hello","please","thanks","thank","yeah","nah",
    "seriously","literally","actually","basically","honestly",
    # Vague/filler words with zero retrieval signal
    "actual","something","anything","everything","nothing","someone","anyone",
    "everyone","nobody","somebody","anyway","random","vibing","lol","lmao",
    "help","sure","really","maybe","ever","never","always","much","many",
    "lot","bit","kind","sort","stuff","things","regardless","whatever",
    "somehow","somewhere","everywhere","nowhere","huh","wtf","bruh","yo",
})


# Regex to find file paths in queries (tool calls pass raw paths as content)
_PATH_RE = re.compile(r"[/~][\w./\-]+")
# Directory names with trailing slash (e.g. "includes/", "layouts/", "backup/")
_DIR_RE = re.compile(r"\b\w+/\s")
# Standalone file extensions like .css .json .bak that leak from listings
_EXT_RE = re.compile(r"\b[\w\-]+\.(?:css|json|jsonc|js|ts|py|md|txt|html|bak|toml|yaml|yml|conf|cfg|log|sh|rs|go|c|h|svg|xml)\b")
# Generic path components with zero retrieval value
_PATH_NOISE = frozenset({
    "home", "usr", "local", "bin", "lib", "etc", "var", "tmp", "opt",
    "config", "configs", "src", "lib", "include", "includes", "build",
    "dist", "node_modules", "cache", "data", "backup", "bak", "layouts",
    "projects", "project", "hdd", "ssd", "dotfiles",
})


def _strip_paths(text: str) -> str:
    """Replace file paths with their meaningful last component(s).
    
    /home/sifat/Projects/odysseus → "odysseus"
    /home/sifat/.config/waybar/style.css → "waybar"
    """
    def _path_replacer(m: re.Match) -> str:
        path = m.group(0)
        # Split path into components, strip extension from last
        parts = [p for p in path.strip("/~").split("/") if p and p != "."]
        if not parts:
            return " "
        # Remove extension from last part
        last = parts[-1]
        if "." in last:
            last = last.rsplit(".", 1)[0]
            parts[-1] = last
        # Filter out noise components, keep meaningful ones (last 2 max)
        meaningful = [p for p in parts if p.lower() not in _PATH_NOISE and len(p) > 2]
        if meaningful:
            return " " + " ".join(meaningful[-2:]) + " "
        return " "

    text = _PATH_RE.sub(_path_replacer, text)
    text = _DIR_RE.sub(" ", text)
    text = _EXT_RE.sub(" ", text)
    return text


def _tokenize(text: str) -> set[str]:
    # Split on non-alphanumeric (including underscores in snake_case keys)
    tokens = re.findall(r"[a-z0-9]+", text.lower())
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
        self._trigger_tokens: list[set[str]] = []  # triggers + tags per entry
        self._source_query_tokens: list[set[str]] = []  # source_query per entry
        self._idf_table: dict[str, float] = {}  # token -> IDF weight
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
        # _trigger_tokens and _idf_table already set by _load_memory_entries
        # which is always called before cache check in _ensure_loaded

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

    def _extract_retrieval_tokens(self, entry: dict) -> tuple[set[str], set[str]]:
        """Extract trigger/tag tokens and source_query tokens from a memory entry."""
        trigger_tokens: set[str] = set()
        for t in entry.get("triggers", []):
            trigger_tokens.update(_tokenize(str(t)))
        for t in entry.get("tags", []):
            trigger_tokens.update(_tokenize(str(t)))
        # Also include key tokens as trigger signal
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

        # Load main memory (all categories including procedural stored here)
        # Procedural.json is ALSO loaded separately below for legacy entries
        if self._memory_path.exists():
            try:
                data = json.loads(self._memory_path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            if isinstance(data, dict):
                for cat_key in ("semantic", "episodic", "ephemeral", "procedural"):
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
                        # Include triggers/tags/source_query in embed text for richer vectors
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
                # Also add procedural trigger/keywords to trigger tokens
                if trigger:
                    trig_toks.update(_tokenize(trigger))
                if isinstance(keywords, list):
                    for kw in keywords:
                        trig_toks.update(_tokenize(str(kw)))
                self._trigger_tokens.append(trig_toks)
                self._source_query_tokens.append(sq_toks)

        # Build IDF table from all trigger/tag tokens
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

    def _idf_trigger_scores(self, query_tokens: set[str]) -> np.ndarray:
        """IDF-weighted trigger/tag matching. Returns per-entry scores."""
        n = len(self._entries)
        scores = np.zeros(n, dtype="float32")
        if len(query_tokens) < 1 or not self._idf_table:
            return scores
        # Adaptive: short queries (<=3 tokens) allow single-token match
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
        """Key-token overlap scoring with substring bonus.
        Adaptive minimum: short queries allow single-token match."""
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
        # Normalize to 0-1
        mx = scores.max()
        if mx > 0:
            scores /= mx
        return scores

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
                return []  # No meaningful tokens — nothing to search
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

            # Weighted fusion - all signals already 0-1, no normalization needed
            scores = (
                TRIGGER_IDF_WEIGHT * trigger_scores
                + SOURCE_QUERY_WEIGHT * sq_scores
                + VECTOR_WEIGHT * vector_scores
                + KEY_TOKEN_WEIGHT * key_scores
                + KEYWORD_WEIGHT * keyword_scores
            )

            # Protected boost — only on strong textual match (trigger or keyword, not source_query)
            for i, meta in enumerate(self._entry_meta):
                if meta["category"] == "protected":
                    has_text_signal = (trigger_scores[i] > 0 or keyword_scores[i] > 0)
                    if has_text_signal:
                        scores[i] += PROTECTED_BOOST

            # Vector-only gate: if NO strong textual signal fired, require very high vector score
            _VECTOR_ONLY_MIN = 0.50
            for i in range(n):
                has_any_text = (trigger_scores[i] > 0 or keyword_scores[i] > 0)
                if not has_any_text and vector_scores[i] < _VECTOR_ONLY_MIN:
                    scores[i] = 0.0

            cutoff = threshold if threshold is not None else self.threshold
            # Adaptive threshold: short queries need stronger evidence
            n_tokens = len(query_tokens)
            if n_tokens <= 1:
                cutoff = max(cutoff, 0.60)
            elif n_tokens <= 2:
                cutoff = max(cutoff, 0.40)
            ranked = np.argsort(-scores)

            # Split-budget selection: procedural priority within total cap
            use_split = top_total is not None
            if use_split:
                _tm = top_memory if top_memory is not None else 5
                _tp = top_procedural if top_procedural is not None else 3
                _tt = top_total
                # Procedural gets priority: fill up to min(top_procedural, top_total)
                # Then other categories fill remaining slots up to top_total
                proc_budget = min(_tp, _tt)
                other_budget = _tt - proc_budget
                # But also respect top_memory cap for non-procedural
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
        {"id": name, "threshold": thresh}
        for name, thresh in MODEL_THRESHOLDS.items()
    ]
