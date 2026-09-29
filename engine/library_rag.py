"""Retrieval-augmented context for long study documents.

study_ai.py currently hands the model a truncated slice of the document
(``_MAX_DOC_CTX = 60_000`` chars), so a long textbook silently loses its tail
-- the one place a student most needs the whole thing. This module chunks the
extracted page text, optionally embeds it, and retrieves only the passages
that matter for a given question.

Two retrieval paths, chosen automatically:
  * vector  -- a local fastembed model is present; cosine top-k over chunks.
  * lexical -- token-overlap scoring with IDF weighting; zero dependencies
               beyond the stdlib, so retrieval always works offline.

Derived data lives in ``library/embeddings/``:
    <doc-id>.json   chunk metadata + text (always written)
    <doc-id>.npy    float32 vectors, one row per chunk (only when embedded)
Both are rebuildable from the page text, so losing them costs only time.
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
from pathlib import Path
from typing import Any, Iterable

from engine.config import LIBRARY_EMBED_DIR

logger = logging.getLogger("sable.study.rag")

_ROOT = Path(__file__).resolve().parent.parent
_EMBED_MODEL = "snowflake/snowflake-arctic-embed-xs"

_CHUNK_CHARS = 1400
_CHUNK_OVERLAP = 200
_MAX_CHUNKS = 4000
_DEFAULT_K = 6
_MAX_CONTEXT_CHARS = 60_000

_TOKEN_RE = re.compile(r"[a-z0-9\u0080-\uffff]+", re.I)
_STOP = frozenset(
    "a an and are as at be but by for from has have if in into is it its of on or "
    "that the their there these this to was were will with what which who how why".split()
)

# Loaded once per process; None means "no local model, use lexical".
_embedder: Any = None
_embedder_probed = False


def _embed_dir() -> Path:
    LIBRARY_EMBED_DIR.mkdir(parents=True, exist_ok=True)
    return LIBRARY_EMBED_DIR


def _load_embedder() -> Any:
    """Best-effort local embedder. Returns None when fastembed is unavailable."""
    global _embedder, _embedder_probed
    if _embedder_probed:
        return _embedder
    _embedder_probed = True
    try:
        from fastembed import TextEmbedding
    except Exception:
        logger.info("fastembed not installed - RAG runs in lexical mode")
        return None
    try:
        cache = _ROOT / "system" / "fastembed_cache"
        cache.mkdir(parents=True, exist_ok=True)
        _embedder = TextEmbedding(model_name=_EMBED_MODEL, cache_dir=str(cache))
        logger.info("RAG embedder ready: %s", _EMBED_MODEL)
    except Exception as exc:
        logger.info("fastembed model unavailable (%s) - lexical mode", exc)
        _embedder = None
    return _embedder


# ── Chunking ────────────────────────────────────────────────────────────────


def _chunk_pages(pages: Iterable[str]) -> list[dict[str, Any]]:
    """Split page text into overlapping character windows, page-anchored.

    Chunks never span a page boundary: a card or AI citation must be able to
    point at one page number.
    """
    chunks: list[dict[str, Any]] = []
    for number, raw in enumerate(pages, start=1):
        text = re.sub(r"\s+", " ", str(raw or "")).strip()
        if not text:
            continue
        if len(text) <= _CHUNK_CHARS:
            chunks.append({"page": number, "text": text})
            continue
        step = max(1, _CHUNK_CHARS - _CHUNK_OVERLAP)
        for start in range(0, len(text), step):
            piece = text[start:start + _CHUNK_CHARS].strip()
            if len(piece) < 80:
                break
            chunks.append({"page": number, "text": piece})
            if len(chunks) >= _MAX_CHUNKS:
                return chunks
    return chunks


def _tokens(text: str) -> list[str]:
    return [t for t in (m.group(0).lower() for m in _TOKEN_RE.finditer(text)) if t not in _STOP and len(t) > 1]


# ── Build ───────────────────────────────────────────────────────────────────


def build(doc_id: str, pages: list[str]) -> dict[str, Any]:
    """Chunk (and embed, when possible) a document. Safe to call repeatedly."""
    if not doc_id or not pages:
        return {"doc_id": doc_id, "chunks": 0, "embedded": False}

    chunks = _chunk_pages(pages)
    if not chunks:
        return {"doc_id": doc_id, "chunks": 0, "embedded": False}

    payload = {
        "version": 1,
        "doc_id": doc_id,
        "built": time.time(),
        "chunks": chunks,
    }
    target = _embed_dir() / f"{doc_id}.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)

    embedded = _write_vectors(doc_id, [c["text"] for c in chunks])
    return {"doc_id": doc_id, "chunks": len(chunks), "embedded": embedded}


def _write_vectors(doc_id: str, texts: list[str]) -> bool:
    """Embed and persist vectors. Returns False when no model is available."""
    model = _load_embedder()
    if model is None:
        (_embed_dir() / f"{doc_id}.npy").unlink(missing_ok=True)
        return False
    try:
        import numpy as np

        vectors = np.asarray(list(model.embed(texts)), dtype="float32")
        np.save(_embed_dir() / f"{doc_id}.npy", vectors)
        return True
    except Exception as exc:
        logger.warning("embedding %s failed: %s", doc_id, exc)
        return False


def load(doc_id: str) -> dict[str, Any] | None:
    path = LIBRARY_EMBED_DIR / f"{doc_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) and isinstance(data.get("chunks"), list) else None


def has_index(doc_id: str) -> bool:
    return (LIBRARY_EMBED_DIR / f"{doc_id}.json").exists()


def remove(doc_id: str) -> None:
    (_embed_dir() / f"{doc_id}.json").unlink(missing_ok=True)
    (_embed_dir() / f"{doc_id}.npy").unlink(missing_ok=True)


# ── Retrieve ────────────────────────────────────────────────────────────────


def retrieve(doc_id: str, query: str, k: int = _DEFAULT_K) -> list[dict[str, Any]]:
    """Top-k passages for a question. Empty list when the doc is unindexed."""
    data = load(doc_id)
    if not data or not str(query or "").strip():
        return []
    chunks: list[dict[str, Any]] = data["chunks"]
    k = max(1, min(int(k or _DEFAULT_K), 40))

    scored = _vector_scores(doc_id, query, chunks)
    if scored is None:
        scored = _lexical_scores(query, chunks)

    top = sorted(scored, key=lambda pair: -pair[1])[:k]
    return [
        {"page": int(chunks[i]["page"]), "text": chunks[i]["text"], "score": round(score, 4)}
        for i, score in top
        if score > 0
    ]


def _vector_scores(doc_id: str, query: str, chunks: list[dict[str, Any]]) -> list[tuple[int, float]] | None:
    model = _load_embedder()
    if model is None:
        return None
    path = LIBRARY_EMBED_DIR / f"{doc_id}.npy"
    if not path.exists():
        return None
    try:
        import numpy as np

        vectors = np.load(path)
        if vectors.shape[0] != len(chunks):
            return None
        qvec = np.asarray(next(iter(model.embed([query]))), dtype="float32")
        denom = (np.linalg.norm(vectors, axis=1) * float(np.linalg.norm(qvec))) + 1e-9
        sims = (vectors @ qvec) / denom
        return [(i, float(s)) for i, s in enumerate(sims)]
    except Exception as exc:
        logger.debug("vector scoring failed for %s: %s", doc_id, exc)
        return None


def _lexical_scores(query: str, chunks: list[dict[str, Any]]) -> list[tuple[int, float]]:
    """IDF-weighted token overlap. Deterministic and dependency-free."""
    q_terms = _tokens(query)
    if not q_terms:
        return []
    tokenised = [_tokens(c["text"]) for c in chunks]
    doc_freq: dict[str, int] = {}
    for tokens in tokenised:
        for term in set(tokens):
            doc_freq[term] = doc_freq.get(term, 0) + 1

    total = max(1, len(chunks))
    scored: list[tuple[int, float]] = []
    for i, tokens in enumerate(tokenised):
        counts: dict[str, int] = {}
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1
        if not counts:
            scored.append((i, 0.0))
            continue
        score = 0.0
        length_norm = 1.0 + math.log(1 + len(tokens) / 200.0)
        for term in q_terms:
            tf = counts.get(term, 0)
            if not tf:
                continue
            idf = math.log(1 + total / (1 + doc_freq.get(term, 0)))
            score += (1 + math.log(tf)) * idf
        scored.append((i, score / length_norm))
    return scored


# ── Context assembly (used by study_ai) ─────────────────────────────────────


def context_for(
    doc_id: str,
    query: str,
    *,
    max_chars: int = _MAX_CONTEXT_CHARS,
    k: int = _DEFAULT_K,
) -> tuple[str, list[int]]:
    """Build a grounded context block for a question.

    Returns ``(context, pages)`` where ``pages`` lists the page numbers that
    contributed, so the caller can cite them. Falls back to an empty string
    when the document has no index, letting the caller use its old path.
    """
    hits = retrieve(doc_id, query, k=k)
    if not hits:
        return "", []

    budget = max(2_000, int(max_chars or _MAX_CONTEXT_CHARS))
    parts: list[str] = []
    pages: list[int] = []
    used = 0
    for hit in hits:
        block = f"[page {hit['page']}]\n{hit['text']}"
        if used + len(block) > budget:
            break
        parts.append(block)
        pages.append(hit["page"])
        used += len(block)
    if not parts:
        return "", []
    return "\n\n---\n\n".join(parts), sorted(set(pages))


def context_for_docs(
    doc_ids: list[str],
    query: str,
    *,
    labels: dict[str, str] | None = None,
    current_doc: str = "",
    max_chars: int = _MAX_CONTEXT_CHARS,
    k: int = _DEFAULT_K,
) -> tuple[str, list[dict[str, Any]]]:
    """Topic-scoped retrieval: merge the top passages across several documents.

    Single-doc ``context_for`` answers "what does this book say"; this answers
    "what does this topic say" by scoring every indexed doc in the topic and
    interleaving the survivors by score. Each hit keeps its ``doc_id`` so the
    caller can cite which book a passage came from.

    ``current_doc`` is the document the reader has open. Its passages are
    tagged ``CURRENT DOCUMENT`` and every other document's ``OTHER DOCUMENT``,
    so the model can tell whether a fact came from the book being read or a
    sibling in the same topic.

    Returns ``(context, hits)``, or ``("", [])`` when nothing is indexed.
    """
    ids = [d for d in dict.fromkeys(doc_ids or []) if d]
    if not ids or not str(query or "").strip():
        return "", []

    # Scale the passage budget with the topic size, then cap it: a six-doc
    # topic earns more room than the single-doc default, but never unbounded.
    total_k = max(_DEFAULT_K, min(24, int(k) * len(ids)))
    per_doc = max(2, total_k // len(ids) + 1)

    merged: list[dict[str, Any]] = []
    for doc_id in ids:
        for hit in retrieve(doc_id, query, k=per_doc):
            merged.append({**hit, "doc_id": doc_id})
    if not merged:
        return "", []

    merged.sort(key=lambda h: -float(h.get("score") or 0.0))
    budget = max(2_000, int(max_chars or _MAX_CONTEXT_CHARS))
    parts: list[str] = []
    hits: list[dict[str, Any]] = []
    used = 0
    for hit in merged:
        label = (labels or {}).get(hit["doc_id"]) or hit["doc_id"]
        origin = "CURRENT DOCUMENT" if hit["doc_id"] == current_doc else "OTHER DOCUMENT"
        block = f"[{origin}: {label} | page {hit['page']}]\n{hit['text']}"
        if used + len(block) > budget:
            break
        parts.append(block)
        hits.append({
            "doc_id": hit["doc_id"],
            "page": int(hit["page"]),
            "score": hit.get("score"),
            "current": hit["doc_id"] == current_doc,
        })
        used += len(block)
        if len(parts) >= total_k:
            break
    if not parts:
        return "", []
    return "\n\n---\n\n".join(parts), hits

