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
from .sable_provider import (
    sable_available,
    sable_multi_search,
    sable_multi_search_traced,
    sable_image_search,
    sable_image_search_traced,
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
    # sable needs no key; it needs the desktop shell's bridge.
    if provider == "sable":
        config["has_api_key"] = sable_available()
    else:
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
    elif provider_name == "sable":
        # Real Chromium tabs in the desktop shell. Not available on the web
        # build; the provider is filtered out of Settings there and this call
        # raises NetworkError so the fallback chain takes over.
        return sable_multi_search(query, count, time_filter)
    return []


def image_search_for_provider_traced(
    provider: str, query: str, count: int = 10
) -> tuple[List[dict], List[dict]]:
    """Image search through one provider, plus its per-engine report.

    Only `sable` has a report — it fuses five indexes. Every other provider is
    a single API, so its report is empty.
    """
    if provider == "sable":
        if not sable_available():
            raise NetworkError("sable search bridge is not running (desktop shell not active)")
        return sable_image_search_traced(query, count)

    return image_search_for_provider(provider, query, count), []


def image_search_for_provider(provider: str, query: str, count: int = 10) -> List[dict]:
    """Image search through ONE named provider.

    Empty list means that provider has no image index or returned nothing —
    never an exception for a merely-unavailable provider, so callers can chain.
    """
    if provider == "sable":
        if not sable_available():
            raise NetworkError("sable search bridge is not running (desktop shell not active)")
        return sable_image_search(query, count)

    if provider == "searxng":
        from .providers import searxng_image_results

        return searxng_image_results(query, count=count)

    from .image_providers import image_search_for

    return image_search_for(provider, query, count)


def image_search_traced(
    query: str, count: int = 10
) -> tuple[List[dict], str, List[dict]]:
    """Image search through the chain, reporting which provider answered.

    Returns ``(results, provider, engines)``. ``provider`` is the one that
    actually served the results — or the configured one when every link
    failed, so a caller can say "configured X, got nothing" instead of
    misreporting X as the source of results that came from elsewhere.
    ``engines`` is the per-engine report when the answering provider was
    `sable`, empty otherwise.
    """
    settings = _get_search_settings()
    provider = settings.get("search_provider", "searxng")

    chain: List[str] = [provider]
    for p in settings.get("search_fallback_chain", []) or []:
        if isinstance(p, str) and p and p not in chain:
            chain.append(p)
    if sable_available() and "sable" not in chain:
        chain.append("sable")
    if "searxng" not in chain:
        chain.append("searxng")

    for candidate in chain:
        try:
            results, engines = image_search_for_provider_traced(candidate, query, count)
            if results:
                logger.info("image search %r served by %s (%d)", query, candidate, len(results))
                return results, candidate, engines
        except Exception as exc:  # noqa: BLE001 - try the next link in the chain
            logger.warning("image search via %s failed: %s", candidate, exc)

    logger.warning("image search %r exhausted its chain with no results", query)
    return [], provider, []


def image_search_results(query: str, count: int = 10) -> List[dict]:
    """Image search through the configured provider, then its fallback chain.

    The configured provider is tried first when it has an image endpoint
    (serper, brave, tavily, google_pse, sable, searxng). Anything after that
    comes from `search_fallback_chain`, then `sable` if the desktop shell is
    up, then SearXNG's image category as the floor.

    Returns ``[{title, image, thumbnail, url}]``; empty when nothing answered.
    """
    results, _provider = image_search_traced(query, count)
    return results

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
