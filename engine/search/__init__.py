"""Multi-provider search engine.

Public API:
    search()              — unified search with caching + retry + fallback
    get_search_config()   — current config (never exposes secrets)
    update_search_config()— merge non-secret config
    invalidate_cache()    — clear cached results
"""

from .core import (
    search,
    searxng_search_results,
    comprehensive_web_search,
    get_search_config,
    update_search_config,
    invalidate_search_cache as invalidate_cache,
)

__all__ = [
    "search",
    "searxng_search_results",
    "comprehensive_web_search",
    "get_search_config",
    "update_search_config",
    "invalidate_cache",
]
