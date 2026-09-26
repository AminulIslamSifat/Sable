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

# Hybrid fusion weights (2026-09-26 benchmark recalibrated).
# Vector-dominant: IDF token-overlap signals normalized query-side and misfired on
# short queries (a single common token scored 1.0). Embeddings proved far more
# robust. Measured on the 100-prompt hand-labelled set: retrieval 33%->44%,
# rejection 50%->60% vs the previous weights (both metrics improved).
VECTOR_WEIGHT = 0.70
KEYWORD_WEIGHT = 0.05
KEY_TOKEN_WEIGHT = 0.05
TRIGGER_IDF_WEIGHT = 0.15
SOURCE_QUERY_WEIGHT = 0.05
PROTECTED_BOOST = 0.15
EPISODIC_DECAY_RATE = 0.95  # Score multiplier per day since last access

# Vector-only gate: if NO strong textual signal fired, require very high vector score
VECTOR_ONLY_MIN = 0.50

# Minimum informative query tokens required before retrieval runs at all.
# Below this, the message carries too little intent and noise dominates.
MIN_QUERY_TOKENS = 3
