"""Scoring constants, model thresholds, and hybrid fusion weights for memory search."""

from __future__ import annotations

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

# Vector-only gate: if NO strong textual signal fired, require very high vector score
VECTOR_ONLY_MIN = 0.50
