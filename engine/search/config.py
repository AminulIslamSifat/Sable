"""Centralized search settings loader.

Reads from system/settings.json with fallback to tools/online_search/scripts/settings.json.
Never caches secrets — keys are resolved on every call.
Supports multiple API keys per provider with round-robin rotation.
"""

import itertools
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_PRIMARY_SETTINGS = _PROJECT_ROOT / "system" / "settings.json"
_FALLBACK_SETTINGS = _PROJECT_ROOT / "tools" / "online_search" / "scripts" / "settings.json"

_SAFESEARCH_LEVELS = ("strict", "moderate", "off")
_FALLBACK_CHAIN_DEFAULT: List[str] = ["duckduckgo"]

def _load_settings_file() -> Dict[str, Any]:
    """Load settings from primary or fallback JSON file."""
    for path in (_PRIMARY_SETTINGS, _FALLBACK_SETTINGS):
        if path.is_file():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning("Failed to load settings from %s: %s", path, e)
    return {}

def _get_search_settings() -> Dict[str, Any]:
    """Return merged search settings dict."""
    return _load_settings_file()

def _get_search_instance() -> str:
    """Return the active search API URL, falling back to env var."""
    settings = _get_search_settings()
    url = (settings.get("search_url") or "").strip()
    if url:
        return url.rstrip("/")
    return os.environ.get("SEARXNG_INSTANCE", "http://localhost:8080").rstrip("/")

# ── Key rotation state ────────────────────────────────────────────────
# Per-provider round-robin iterators, lazily initialized.
_key_rotators: Dict[str, itertools.cycle] = {}
_key_rotator_lock = threading.Lock()

_SETTINGS_FIELD_MAP = {
    "brave": "brave_api_key",
    "google_pse": "google_pse_key",
    "tavily": "tavily_api_key",
    "serper": "serper_api_key",
}
_ENV_VAR_MAP = {
    "brave": "DATA_BRAVE_API_KEY",
    "google_pse": "GOOGLE_API_KEY",
    "tavily": "TAVILY_API_KEY",
    "serper": "SERPER_API_KEY",
}


def _resolve_provider_keys(provider: str) -> List[str]:
    """Resolve ALL API keys for a provider (list). Backwards-compatible with single string.

    Priority: settings field (string or list) -> env var -> legacy search_api_key.
    """
    settings = _get_search_settings()
    keys: List[str] = []

    field = _SETTINGS_FIELD_MAP.get(provider, "")
    if field:
        raw = settings.get(field)
        if isinstance(raw, list):
            keys = [k.strip() for k in raw if isinstance(k, str) and k.strip()]
        elif isinstance(raw, str) and raw.strip():
            keys = [raw.strip()]

    # Env var fallback (single key)
    if not keys:
        env_name = _ENV_VAR_MAP.get(provider, "")
        if env_name:
            env_val = (os.environ.get(env_name) or "").strip()
            if env_val:
                keys = [env_val]

    # Legacy shared key
    if not keys:
        legacy = (settings.get("search_api_key") or "").strip()
        if legacy:
            keys = [legacy]

    return keys


def get_provider_keys(provider: str) -> List[str]:
    """Return all configured API keys for a provider."""
    return _resolve_provider_keys(provider)


def _get_provider_key(provider: str) -> str:
    """Resolve next API key via round-robin rotation. Backwards-compatible single-key API."""
    keys = _resolve_provider_keys(provider)
    if not keys:
        return ""
    if len(keys) == 1:
        return keys[0]

    # Round-robin for multiple keys
    with _key_rotator_lock:
        rotator = _key_rotators.get(provider)
        if rotator is None:
            rotator = itertools.cycle(keys)
            _key_rotators[provider] = rotator
        return next(rotator)


def reset_key_rotation(provider: str | None = None) -> None:
    """Reset round-robin state. None resets all providers."""
    with _key_rotator_lock:
        if provider is None:
            _key_rotators.clear()
        else:
            _key_rotators.pop(provider, None)

def _get_result_count() -> int:
    """Return configured result count, default 5."""
    settings = _get_search_settings()
    try:
        return int(settings.get("search_result_count", 5))
    except (ValueError, TypeError):
        return 5

def _get_safesearch_level() -> str:
    """Return normalized SafeSearch level: strict | moderate | off."""
    settings = _get_search_settings()
    raw = (settings.get("search_safesearch") or "strict").strip().lower()
    if raw in _SAFESEARCH_LEVELS:
        return raw
    aliases = {
        "on": "strict", "high": "strict", "2": "strict",
        "medium": "moderate", "1": "moderate", "default": "moderate",
        "none": "off", "disabled": "off", "0": "off",
    }
    return aliases.get(raw, "strict")

def _safesearch_for(provider: str) -> Optional[str]:
    """Translate canonical SafeSearch level into provider-specific value."""
    level = _get_safesearch_level()
    if provider == "searxng":
        return {"strict": "2", "moderate": "1", "off": "0"}[level]
    if provider == "brave":
        return level
    if provider == "duckduckgo_lib":
        return {"strict": "on", "moderate": "moderate", "off": "off"}[level]
    if provider == "duckduckgo_html":
        return {"strict": "1", "moderate": "-1", "off": "-2"}[level]
    if provider == "google_pse":
        return None if level == "off" else "active"
    if provider == "serper":
        return None if level == "off" else "active"
    return None

def _build_provider_chain(primary: str) -> List[str]:
    """Build ordered list: primary first, then configured/default fallbacks."""
    chain = [primary]
    settings = _get_search_settings()
    user_chain = settings.get("search_fallback_chain") or []
    if isinstance(user_chain, str):
        user_chain = [s.strip() for s in user_chain.split(",") if s.strip()]
    fallbacks = user_chain if user_chain else _FALLBACK_CHAIN_DEFAULT
    for fb in fallbacks:
        if fb and fb != primary and fb not in chain and fb != "disabled":
            chain.append(fb)
    return chain
