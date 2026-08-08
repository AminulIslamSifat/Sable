"""Search provider implementations: SearXNG, Brave, DuckDuckGo, Google PSE, Tavily, Serper."""

import json
import logging
import os
from typing import List, Optional
from urllib.parse import urljoin, urlparse, parse_qs

import httpx
from bs4 import BeautifulSoup

from .config import (
    _get_search_settings,
    _get_search_instance,
    _get_provider_key,
    _get_result_count,
    _safesearch_for,
)
from .query import build_enhanced_query

logger = logging.getLogger(__name__)

# ── Exception classes ──

class NetworkError(Exception):
    """HTTP/network failure during search."""

class ParseError(Exception):
    """Failed to parse search response."""

class RateLimitError(Exception):
    """Provider rate-limited the request."""

# Provider registry — maps setting value to (label, needs_key, needs_url)
PROVIDER_INFO = {
    "searxng":    ("SearXNG",      False, True),
    "brave":      ("Brave Search", True,  False),
    "duckduckgo": ("DuckDuckGo",   False, False),
    "google_pse": ("Google PSE",   True,  False),
    "tavily":     ("Tavily",       True,  False),
    "serper":     ("Serper",       True,  False),
    "disabled":   ("Disabled",     False, False),
}

REQUEST_TIMEOUT = 15
WEB_FETCH_USER_AGENT = "Mozilla/5.0 (compatible; SableBot/1.0)"
# DuckDuckGo's html endpoint returns a 202 anomaly/challenge page for bot,
# curl and Firefox UAs — only Chrome-style UAs get real results.
DDG_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)
# Throttle DDG requests — bursts trip their per-IP anomaly CAPTCHA.
_DDG_MIN_INTERVAL = 4.0
_last_ddg_request_ts = 0.0

_NEWS_HINTS = ("news", "nyheter", "headlines", "breaking", "latest", "today", "idag")
_GENERAL_ENGINES = os.environ.get("SEARXNG_GENERAL_ENGINES", "bing,mojeek,presearch")

# ── SearXNG JSON API ──

def searxng_search_api(query: str, count: Optional[int] = None, categories: str = "general",
                       time_filter: Optional[str] = None) -> List[dict]:
    """Search using SearXNG JSON API. Returns list of {title, url, snippet}."""
    count = count if count is not None else _get_result_count()
    instance = _get_search_instance()
    headers = {"User-Agent": WEB_FETCH_USER_AGENT}

    params = {
        "q": query,
        "format": "json",
        "language": "en",
        "safesearch": _safesearch_for("searxng"),
    }
    q_lc = query.lower()
    is_news = time_filter is not None or any(h in q_lc for h in _NEWS_HINTS)
    if is_news and categories == "general":
        params["categories"] = "news"
        if time_filter in ("day", "week", "month", "year"):
            params["time_range"] = "week" if time_filter in ("day", "week") else time_filter
    else:
        params["categories"] = categories
        if categories == "general" and _GENERAL_ENGINES:
            params["engines"] = _GENERAL_ENGINES

    try:
        def _parse_results(results):
            return [
                {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
                for r in results[:count]
                if r.get("url")
            ]

        def _run(search_params):
            response = httpx.get(
                f"{instance}/search", params=search_params,
                headers=headers or None, timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()
            return _parse_results(data.get("results", [])), data

        active_params = params
        parsed, data = _run(active_params)

        # Fallback: news → general engines
        if not parsed and is_news and categories == "general":
            fallback = {
                "q": query, "format": "json", "language": "en",
                "categories": "general", "safesearch": _safesearch_for("searxng"),
            }
            if _GENERAL_ENGINES:
                fallback["engines"] = _GENERAL_ENGINES
            logger.info("SearXNG news search returned 0 results for %r; retrying general engines", query)
            active_params = fallback
            parsed, data = _run(active_params)

        # Fallback: drop language pin
        if not parsed and active_params.get("language"):
            fallback = dict(active_params)
            fallback.pop("language", None)
            logger.info("SearXNG language-pinned search returned 0 for %r; retrying without language", query)
            active_params = fallback
            parsed, data = _run(active_params)

        # Fallback: drop pinned engines
        if not parsed and active_params.get("engines"):
            fallback = dict(active_params)
            fallback.pop("engines", None)
            logger.info("SearXNG pinned engines returned 0 for %r; retrying default engines", query)
            parsed, data = _run(fallback)

        logger.info("SearXNG JSON API returned %d results for: %s", len(parsed), query)
        if not parsed:
            unresponsive = data.get("unresponsive_engines") if isinstance(data, dict) else None
            if unresponsive:
                logger.info("SearXNG unresponsive engines for %r: %s", query, unresponsive)
        return parsed
    except Exception as e:
        logger.warning("SearXNG JSON API search failed: %s", e)
        html_results = searxng_search(query, max_results=count)
        if html_results:
            logger.info("SearXNG HTML fallback returned %d results for: %s", len(html_results), query)
        return html_results

# ── SearXNG HTML fallback ──

def searxng_search(query: str, max_results: int = 10) -> List[dict]:
    """Search using SearXNG instance - parsing HTML."""
    instance = _get_search_instance()
    req_headers = {"User-Agent": WEB_FETCH_USER_AGENT}
    try:
        response = httpx.get(
            f"{instance}/search",
            params={"q": query, "language": "en", "safesearch": _safesearch_for("searxng")},
            headers=req_headers, timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        results = []
        for item in soup.select(".result")[:max_results]:
            title_tag = item.select_one("h3 a")
            snippet_tag = item.select_one(".content")
            if title_tag:
                results.append({
                    "title": title_tag.get_text(strip=True),
                    "url": title_tag.get("href", ""),
                    "snippet": snippet_tag.get_text(strip=True) if snippet_tag else "",
                })
        return results
    except Exception as e:
        logger.warning("SearXNG HTML search failed: %s", e)
        return []

# ── Brave Search ──

def brave_search(query: str, count: int = 5, time_filter: Optional[str] = None) -> List[dict]:
    """Search using Brave Search API."""
    api_key = _get_provider_key("brave")
    if not api_key:
        logger.warning("Brave API key not configured")
        return []
    enhanced = build_enhanced_query(query, time_filter)
    params = {"q": enhanced, "count": min(count, 20)}
    if time_filter:
        freshness_map = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}
        if time_filter in freshness_map:
            params["freshness"] = freshness_map[time_filter]
    ss = _safesearch_for("brave")
    if ss:
        params["safesearch"] = ss
    try:
        response = httpx.get(
            "https://api.search.brave.com/res/v1/web/search",
            params=params,
            headers={"Accept": "application/json", "Accept-Encoding": "gzip", "X-Subscription-Token": api_key},
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code == 429:
            raise RateLimitError("Brave rate limit exceeded")
        response.raise_for_status()
        data = response.json()
        results = []
        for item in data.get("web", {}).get("results", [])[:count]:
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("description", ""),
            })
        logger.info("Brave returned %d results for: %s", len(results), query)
        return results
    except RateLimitError:
        raise
    except Exception as e:
        logger.warning("Brave search failed: %s", e)
        return []

# ── DuckDuckGo ──

def duckduckgo_search(query: str, count: int = 5, time_filter: Optional[str] = None) -> List[dict]:
    """Search using DuckDuckGo HTML (no API key required).

    DDG serves an image CAPTCHA ("select the ducks") to IPs that fire
    requests too fast, so we throttle to one request per _DDG_MIN_INTERVAL.
    """
    global _last_ddg_request_ts
    import time as _time

    wait = _DDG_MIN_INTERVAL - (_time.monotonic() - _last_ddg_request_ts)
    if wait > 0:
        _time.sleep(wait)
    _last_ddg_request_ts = _time.monotonic()

    enhanced = build_enhanced_query(query, time_filter)
    params = {"q": enhanced, "kl": "us-en"}
    ss = _safesearch_for("duckduckgo_html")
    if ss:
        params["kp"] = ss
    try:
        response = httpx.get(
            "https://html.duckduckgo.com/html/",
            params=params,
            headers={"User-Agent": DDG_BROWSER_USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code == 202:
            raise RateLimitError("DuckDuckGo anomaly detection triggered (202)")
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        results = []
        for item in soup.select(".result")[:count]:
            title_tag = item.select_one(".result__a")
            snippet_tag = item.select_one(".result__snippet")
            if title_tag:
                href = title_tag.get("href", "")
                # DDG wraps URLs in a redirect
                if "uddg=" in href:
                    parsed = parse_qs(urlparse(href).query)
                    href = parsed.get("uddg", [href])[0]
                results.append({
                    "title": title_tag.get_text(strip=True),
                    "url": href,
                    "snippet": snippet_tag.get_text(strip=True) if snippet_tag else "",
                })
        logger.info("DuckDuckGo returned %d results for: %s", len(results), query)
        return results
    except Exception as e:
        logger.warning("DuckDuckGo search failed: %s", e)
        return []

# ── Google PSE ──

def google_pse_search(query: str, count: int = 5, time_filter: Optional[str] = None) -> List[dict]:
    """Search using Google Custom Search JSON API."""
    api_key = _get_provider_key("google_pse")
    if not api_key:
        logger.warning("Google PSE API key not configured")
        return []
    settings = _get_search_settings()
    cx = (settings.get("google_pse_cx") or os.environ.get("GOOGLE_CSE_ID", "")).strip()
    if not cx:
        logger.warning("Google PSE CX not configured")
        return []
    enhanced = build_enhanced_query(query, time_filter)
    params = {"key": api_key, "cx": cx, "q": enhanced, "num": min(count, 10)}
    ss = _safesearch_for("google_pse")
    if ss:
        params["safe"] = ss
    if time_filter:
        date_restrict_map = {"day": "d1", "week": "w1", "month": "m1", "year": "y1"}
        if time_filter in date_restrict_map:
            params["dateRestrict"] = date_restrict_map[time_filter]
    try:
        response = httpx.get(
            "https://www.googleapis.com/customsearch/v1",
            params=params, timeout=REQUEST_TIMEOUT,
        )
        if response.status_code == 429:
            raise RateLimitError("Google PSE rate limit exceeded")
        response.raise_for_status()
        data = response.json()
        results = []
        for item in data.get("items", [])[:count]:
            results.append({
                "title": item.get("title", ""),
                "url": item.get("link", ""),
                "snippet": item.get("snippet", ""),
            })
        logger.info("Google PSE returned %d results for: %s", len(results), query)
        return results
    except RateLimitError:
        raise
    except Exception as e:
        logger.warning("Google PSE search failed: %s", e)
        return []

# ── Tavily ──

def tavily_search(query: str, count: int = 5, time_filter: Optional[str] = None) -> List[dict]:
    """Search using Tavily API."""
    api_key = _get_provider_key("tavily")
    if not api_key:
        logger.warning("Tavily API key not configured")
        return []
    enhanced = build_enhanced_query(query, time_filter)
    payload = {"api_key": api_key, "query": enhanced, "max_results": count, "include_answer": False}
    if time_filter:
        topic_map = {"day": "news", "week": "news", "month": "general", "year": "general"}
        payload["topic"] = topic_map.get(time_filter, "general")
    try:
        response = httpx.post(
            "https://api.tavily.com/search",
            json=payload, timeout=REQUEST_TIMEOUT,
        )
        if response.status_code == 429:
            raise RateLimitError("Tavily rate limit exceeded")
        response.raise_for_status()
        data = response.json()
        results = []
        for item in data.get("results", [])[:count]:
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", ""),
            })
        logger.info("Tavily returned %d results for: %s", len(results), query)
        return results
    except RateLimitError:
        raise
    except Exception as e:
        logger.warning("Tavily search failed: %s", e)
        return []

# ── Serper ──

def serper_search(query: str, count: int = 5, time_filter: Optional[str] = None) -> List[dict]:
    """Search using Serper.dev API."""
    api_key = _get_provider_key("serper")
    if not api_key:
        logger.warning("Serper API key not configured")
        return []
    enhanced = build_enhanced_query(query, time_filter)
    payload = {"q": enhanced, "num": count}
    if time_filter:
        tbs_map = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}
        if time_filter in tbs_map:
            payload["tbs"] = tbs_map[time_filter]
    try:
        response = httpx.post(
            "https://google.serper.dev/search",
            json=payload,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code == 429:
            raise RateLimitError("Serper rate limit exceeded")
        response.raise_for_status()
        data = response.json()
        results = []
        for item in data.get("organic", [])[:count]:
            results.append({
                "title": item.get("title", ""),
                "url": item.get("link", ""),
                "snippet": item.get("snippet", ""),
            })
        logger.info("Serper returned %d results for: %s", len(results), query)
        return results
    except RateLimitError:
        raise
    except Exception as e:
        logger.warning("Serper search failed: %s", e)
        return []
