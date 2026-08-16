"""engine.memory_search — split into tokenize/scoring/cache/embedder/searcher submodules.

All public names are re-exported here so existing imports like
`from engine.memory_search import get_searcher` continue to work unchanged.
"""

from .searcher import (
    MemorySearcher,
    get_searcher,
    get_project_searcher,
    reload_project_searcher,
    list_available_models,
    _project_searchers,
)
from .scoring import MODEL_THRESHOLDS, DEFAULT_MODEL, DEFAULT_TOP_K
from .tokenize import _tokenize, _strip_paths, _keyword_score, _is_expired
from .cache import _cache_path_for
from .embedder import _GeminiEmbedder, _load_gemini_keys

__all__ = [
    "MemorySearcher",
    "get_searcher",
    "get_project_searcher",
    "reload_project_searcher",
    "list_available_models",
    "_project_searchers",
    "MODEL_THRESHOLDS",
    "DEFAULT_MODEL",
    "DEFAULT_TOP_K",
    "_tokenize",
    "_strip_paths",
    "_keyword_score",
    "_is_expired",
    "_cache_path_for",
    "_GeminiEmbedder",
    "_load_gemini_keys",
]
