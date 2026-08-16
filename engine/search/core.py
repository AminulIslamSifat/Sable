"""Core search orchestrators: searxng_search_results, comprehensive_web_search, config, cache invalidation."""

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set

from .cache import (
    SEARCH_CACHE_DIR,
    search_cache_index,
    generate_cache_key,
    cleanup_cache,
)
from .config import (
    _get_search_settings,
    _get_provider_key,
    _get_result_count,
    _build_provider_chain,
)
from .providers import (
    NetworkError,
    ParseError,
    RateLimitError,
    searxng_search_api,
    brave_search,
    duckduckgo_search,
    google_pse_search,
    tavily_search,
    serper_search,
)
from .query import _cache_duration_for_query
from .ranking import rank_search_results

logger = logging.getLogger(__name__)

# ========= CONFIG =========
SEARCH_CONFIG: Dict[str, Any] = {
    "primary_provider": "searxng",
}

def _is_secret_key(name: str) -> bool:
    """True for config keys that hold a credential."""
    return name.endswith(("_api_key", "_key", "_token", "_secret"))

def get_search_config() -> Dict[str, Any]:
    """Get current search configuration including active provider info.

    Never returns stored API keys — only key presence via has_api_key.
    """
    config = SEARCH_CONFIG.copy()
    settings = _get_search_settings()
    provider = settings.get("search_provider", "searxng")
    config["active_provider"] = provider
    config["has_api_key"] = bool(_get_provider_key(provider))
    config["result_count"] = _get_result_count()
    if provider == "searxng":
        from .config import _get_search_instance
        config["search_url"] = _get_search_instance()
    return {
        k: v for k, v in config.items()
        if not (isinstance(v, str) and _is_secret_key(k))
    }

def update_search_config(api_key: str = None, **kwargs):
    """Merge non-secret search config into SEARCH_CONFIG.

    Provider API keys are NOT cached here — read on demand via _get_provider_key.
    """
    for k, v in kwargs.items():
        if not _is_secret_key(k):
            SEARCH_CONFIG[k] = v

def _call_provider(provider_name: str, query: str, count: int, time_filter: str = None) -> List[dict]:
    """Call a search provider by name. Returns list of results or empty list."""
    if provider_name == "searxng":
        return searxng_search_api(query, count, time_filter=time_filter)
    elif provider_name == "brave":
        return brave_search(query, count, time_filter)
    elif provider_name == "duckduckgo":
        return duckduckgo_search(query, count, time_filter)
    elif provider_name == "google_pse":
        return google_pse_search(query, count, time_filter)
    elif provider_name == "tavily":
        return tavily_search(query, count, time_filter)
    elif provider_name == "serper":
        return serper_search(query, count, time_filter)
    return []

# ----------------------------------------------------------------------
# Unified search with caching and retry
# ----------------------------------------------------------------------
def searxng_search_results(query: str, count: int = 10, time_filter: str = None) -> List[dict]:
    """Perform a web search using configured provider with caching and retry."""
    settings = _get_search_settings()
    search_provider = settings.get("search_provider", "searxng")
    result_count = _get_result_count()
    if count == 10:
        count = result_count

    cache_key = generate_cache_key(f"{query}|{count}|{time_filter}")
    cache_file = SEARCH_CACHE_DIR / f"{cache_key}.cache"

    # Check cache
    if cache_file.exists():
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cached_data = json.load(f)
            expiry_raw = cached_data.get("expiry")
            expiry = datetime.fromisoformat(expiry_raw) if expiry_raw else None
            if expiry and datetime.now() < expiry:
                logger.debug("Search cache hit for query: %s", query)
                return cached_data["data"]
            else:
                cache_file.unlink(missing_ok=True)
                search_cache_index.pop(cache_key, None)
        except Exception as e:
            logger.warning("Failed to read search cache for %s: %s", query, e)
            cache_file.unlink(missing_ok=True)
            search_cache_index.pop(cache_key, None)

    logger.debug("Search cache miss for query: %s", query)

    if search_provider == "disabled":
        logger.info("Search is disabled via admin settings")
        return []

    provider_chain = _build_provider_chain(search_provider)

    results: List[dict] = []
    for provider_name in provider_chain:
        for attempt in range(2):
            try:
                logger.info("Attempting %s search (attempt %d)", provider_name, attempt + 1)
                results = _call_provider(provider_name, query, count, time_filter)
                if results:
                    logger.info("%s search succeeded with %d results", provider_name, len(results))
                    break
            except (NetworkError, ParseError, RateLimitError) as e:
                logger.error("%s search error (attempt %d): %s", provider_name, attempt + 1, e)
            except Exception as e:
                logger.error("Unexpected error during %s search (attempt %d): %s", provider_name, attempt + 1, e)
        if results:
            break

    success = bool(results)

    if success:
        results = rank_search_results(query, results)
        try:
            expiry = datetime.now() + _cache_duration_for_query(query)
            cache_data = {
                "timestamp": datetime.now().isoformat(),
                "expiry": expiry.isoformat(),
                "data": results,
            }
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(cache_data, f)
            search_cache_index[cache_key] = datetime.now()
            cleanup_cache(SEARCH_CACHE_DIR, search_cache_index, timedelta(hours=1))
        except Exception as e:
            logger.warning("Failed to write search cache for %s: %s", query, e)

    if not success:
        logger.error("All search providers failed for query: %s", query)

    return results

# ----------------------------------------------------------------------
# Cache invalidation
# ----------------------------------------------------------------------
def invalidate_search_cache(query: Optional[str] = None) -> None:
    """Invalidate cached search results. None clears all, otherwise just the given query."""
    if query is None:
        for file in SEARCH_CACHE_DIR.glob("*.cache"):
            try:
                file.unlink(missing_ok=True)
            except Exception as e:
                logger.warning("Failed to delete cache file %s: %s", file, e)
        search_cache_index.clear()
        logger.info("All search cache entries have been cleared.")
    else:
        cache_key = generate_cache_key(f"{query}|{_get_result_count()}|None")
        cache_file = SEARCH_CACHE_DIR / f"{cache_key}.cache"
        if cache_file.exists():
            try:
                cache_file.unlink(missing_ok=True)
                search_cache_index.pop(cache_key, None)
                logger.info("Cache entry for query '%s' has been invalidated.", query)
            except Exception as e:
                logger.warning("Failed to delete cache file for query '%s': %s", query, e)
        else:
            logger.info("No cache entry found for query '%s'.", query)

# ----------------------------------------------------------------------
# Comprehensive web search (with advanced filtering)
# ----------------------------------------------------------------------
def comprehensive_web_search(
    query: str,
    max_pages: int = 3,
    max_workers: int = 4,
    time_filter: str = None,
    domain_whitelist: Optional[Set[str]] = None,
    domain_blacklist: Optional[Set[str]] = None,
    content_type: Optional[str] = None,
    language: Optional[str] = None,
    min_content_length: int = 100,
) -> List[dict]:
    """Perform comprehensive web search with filtering.

    Phase 1: returns raw search results without content fetching.
    Content extraction stays in online_search.py.
    """
    results = searxng_search_results(query, count=max_pages * 3, time_filter=time_filter)

    # Apply domain filters
    if domain_whitelist:
        from urllib.parse import urlparse
        results = [
            r for r in results
            if urlparse(r.get("url", "")).netloc.lower() in domain_whitelist
        ]
    if domain_blacklist:
        from urllib.parse import urlparse
        results = [
            r for r in results
            if urlparse(r.get("url", "")).netloc.lower() not in domain_blacklist
        ]

    return results[:max_pages]

# Public alias
search = searxng_search_results
