"""Image search across the keyed providers.

Each provider has its own image endpoint and its own response shape; this
module normalises them all to the same unit the rest of the codebase uses:

    {"title", "image", "thumbnail", "url"}

``image``    the media URL to embed
``thumbnail`` a lighter preview (falls back to ``image``)
``url``      the page the image came from

Multi-key rotation on 429 mirrors the text providers — a provider configured
with several keys should not fail because one of them is throttled.
"""

from __future__ import annotations

import logging
from typing import List, Optional

import httpx

from .config import get_provider_keys
from .providers import NetworkError, RateLimitError, REQUEST_TIMEOUT

logger = logging.getLogger(__name__)


def _norm(title: str, image: str, thumb: str, page: str) -> Optional[dict]:
    image = (image or "").strip()
    if not image or not image.startswith(("http://", "https://")):
        return None
    return {
        "title": (title or "").strip(),
        "image": image,
        "thumbnail": (thumb or image).strip() or image,
        "url": (page or "").strip(),
    }


# ── Tavily ──────────────────────────────────────────────────────────────────


def tavily_image_search(query: str, count: int = 10) -> List[dict]:
    """Tavily returns images alongside web results when include_images is set.

    The array is plain URLs by default; with include_image_descriptions it
    becomes [{url, description}]. Both shapes are handled.
    """
    keys = get_provider_keys("tavily")
    if not keys:
        logger.warning("Tavily API key not configured for image search")
        return []

    for attempt, api_key in enumerate(keys):
        payload = {
            "api_key": api_key,
            "query": query,
            "max_results": max(1, min(count, 20)),
            "include_images": True,
            "include_image_descriptions": True,
            "include_answer": False,
        }
        try:
            response = httpx.post(
                "https://api.tavily.com/search", json=payload, timeout=REQUEST_TIMEOUT
            )
            if response.status_code == 429:
                if attempt < len(keys) - 1:
                    continue
                raise RateLimitError("Tavily image rate limit exceeded (all keys exhausted)")
            response.raise_for_status()
            data = response.json()

            out: List[dict] = []
            for item in data.get("images", []) or []:
                if isinstance(item, str):
                    row = _norm("", item, item, "")
                elif isinstance(item, dict):
                    u = item.get("url") or ""
                    row = _norm(item.get("description", ""), u, u, "")
                else:
                    continue
                if row:
                    out.append(row)

            logger.info("Tavily images: %d for %r (key %d/%d)", len(out), query, attempt + 1, len(keys))
            return out[:count]
        except RateLimitError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("Tavily image search failed (key %d/%d): %s", attempt + 1, len(keys), e)
            if attempt < len(keys) - 1:
                continue
            return []
    return []


# ── Serper ──────────────────────────────────────────────────────────────────


def serper_image_search(query: str, count: int = 10) -> List[dict]:
    """Serper proxies Google Images. POST https://google.serper.dev/images.

    Each item carries imageUrl (full), thumbnailUrl (preview), link (source
    page), title, source and domain.
    """
    keys = get_provider_keys("serper")
    if not keys:
        logger.warning("Serper API key not configured for image search")
        return []

    for attempt, api_key in enumerate(keys):
        payload = {"q": query, "num": max(1, min(count, 100))}
        try:
            response = httpx.post(
                "https://google.serper.dev/images",
                json=payload,
                headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code == 429:
                if attempt < len(keys) - 1:
                    continue
                raise RateLimitError("Serper image rate limit exceeded (all keys exhausted)")
            response.raise_for_status()
            data = response.json()

            out: List[dict] = []
            for item in data.get("images", []) or []:
                if not isinstance(item, dict):
                    continue
                row = _norm(
                    item.get("title", ""),
                    item.get("imageUrl", ""),
                    item.get("thumbnailUrl", ""),
                    item.get("link", ""),
                )
                if row:
                    out.append(row)

            logger.info("Serper images: %d for %r (key %d/%d)", len(out), query, attempt + 1, len(keys))
            return out[:count]
        except RateLimitError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("Serper image search failed (key %d/%d): %s", attempt + 1, len(keys), e)
            if attempt < len(keys) - 1:
                continue
            return []
    return []


# ── Brave ───────────────────────────────────────────────────────────────────


def brave_image_search(query: str, count: int = 10) -> List[dict]:
    """Brave's image endpoint. GET /res/v1/images/search.

    Items carry properties.url (source page) and thumbnail.src (the image).
    """
    keys = get_provider_keys("brave")
    if not keys:
        logger.warning("Brave API key not configured for image search")
        return []

    for attempt, api_key in enumerate(keys):
        params = {"q": query, "count": max(1, min(count, 100)), "safesearch": "strict"}
        try:
            response = httpx.get(
                "https://api.search.brave.com/res/v1/images/search",
                params=params,
                headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code == 429:
                if attempt < len(keys) - 1:
                    continue
                raise RateLimitError("Brave image rate limit exceeded (all keys exhausted)")
            response.raise_for_status()
            data = response.json()

            out: List[dict] = []
            for item in data.get("results", []) or []:
                if not isinstance(item, dict):
                    continue
                thumb = item.get("thumbnail") or {}
                props = item.get("properties") or {}
                row = _norm(
                    item.get("title", ""),
                    props.get("url") or thumb.get("src", ""),
                    thumb.get("src", ""),
                    item.get("url", ""),
                )
                if row:
                    out.append(row)

            logger.info("Brave images: %d for %r (key %d/%d)", len(out), query, attempt + 1, len(keys))
            return out[:count]
        except RateLimitError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("Brave image search failed (key %d/%d): %s", attempt + 1, len(keys), e)
            if attempt < len(keys) - 1:
                continue
            return []
    return []


# ── Google Programmable Search ──────────────────────────────────────────────


def google_pse_image_search(query: str, count: int = 10) -> List[dict]:
    """Google PSE with searchType=image.

    PSE returns a link (the source page) and image.thumbnailLink; there is no
    reliable full-res field, so the thumbnail is the media URL.
    """
    keys = get_provider_keys("google_pse")
    if not keys:
        logger.warning("Google PSE key not configured for image search")
        return []

    settings = _pse_cx()
    if not settings:
        logger.warning("Google PSE cx not configured for image search")
        return []

    for attempt, api_key in enumerate(keys):
        params = {
            "key": api_key,
            "cx": settings,
            "q": query,
            "searchType": "image",
            "num": max(1, min(count, 10)),
            "safe": "active",
        }
        try:
            response = httpx.get(
                "https://www.googleapis.com/customsearch/v1", params=params, timeout=REQUEST_TIMEOUT
            )
            if response.status_code == 429:
                if attempt < len(keys) - 1:
                    continue
                raise RateLimitError("Google PSE image rate limit exceeded (all keys exhausted)")
            response.raise_for_status()
            data = response.json()

            out: List[dict] = []
            for item in data.get("items", []) or []:
                if not isinstance(item, dict):
                    continue
                img = item.get("image") or {}
                row = _norm(
                    item.get("title", ""),
                    img.get("thumbnailLink", ""),
                    img.get("thumbnailLink", ""),
                    item.get("link", ""),
                )
                if row:
                    out.append(row)

            logger.info("Google PSE images: %d for %r (key %d/%d)", len(out), query, attempt + 1, len(keys))
            return out[:count]
        except RateLimitError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("Google PSE image search failed (key %d/%d): %s", attempt + 1, len(keys), e)
            if attempt < len(keys) - 1:
                continue
            return []
    return []


def _pse_cx() -> str:
    from .config import _get_search_settings

    return (_get_search_settings().get("google_pse_cx") or "").strip()


# ── registry ────────────────────────────────────────────────────────────────

# Ordered by expected image quality: Serper proxies the Google index, Brave has
# its own, Tavily is general-purpose, PSE is capped at 10 per call.
IMAGE_PROVIDERS = {
    "serper": serper_image_search,
    "brave": brave_image_search,
    "tavily": tavily_image_search,
    "google_pse": google_pse_image_search,
}


def has_image_provider(provider: str) -> bool:
    """True when this provider name has an image endpoint at all."""
    return provider in IMAGE_PROVIDERS


def image_provider_unavailable_reason(provider: str) -> str:
    """Why a provider would return nothing, for the settings test panel.

    An empty list is ambiguous — no key, no cx, or a genuinely empty index.
    The test button exists to tell those apart, so say which it is.
    """
    if provider not in IMAGE_PROVIDERS:
        return f"{provider} has no image endpoint"
    if not get_provider_keys(provider):
        return f"{provider} has no API key configured"
    if provider == "google_pse" and not _pse_cx():
        return "google_pse has no cx (search engine id) configured"
    return ""


def image_search_for(provider: str, query: str, count: int = 10) -> List[dict]:
    """Run one keyed provider's image search. Unknown provider -> empty list."""
    fn = IMAGE_PROVIDERS.get(provider)
    if not fn:
        return []
    try:
        return fn(query, count)
    except RateLimitError as e:
        logger.warning("%s image search rate-limited: %s", provider, e)
        return []
    except NetworkError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning("%s image search failed: %s", provider, e)
        return []
