
"""Connector registry — maps api_backend names to connector instances.

Usage in chat routes:
    from connectors import get_connector, resolve_backend

    backend = resolve_backend(model_id)   # "deepseek" | "gemini" | None
    if backend:
        connector = get_connector(backend)
        async for event in connector.stream_chat(...):
            ...
"""

from __future__ import annotations

import logging
from typing import Any

from engine.config import get_model_config

logger = logging.getLogger("sable.connectors")

# Lazy-loaded connector singletons, keyed by api_backend name.
_registry: dict[str, Any] = {}


def resolve_backend(model_id: str | None) -> str | None:
    """Return the api_backend string for a model, or None if it's a Qwen/scraper model."""
    if not model_id:
        return None
    cfg = get_model_config(model_id)
    return cfg.get("api_backend")  # None for Qwen models


def get_connector(backend: str, model_id: str | None = None) -> Any:
    """Get (or lazily create) the connector instance for a backend name.

    For 'local' backend, pass model_id to resolve the correct endpoint.
    Raises KeyError if the backend has no registered connector.
    """
    # Local backend needs per-model endpoint resolution
    if backend == "local" and model_id:
        from connectors.local.client import get_client
        cfg = get_model_config(model_id)
        endpoint = cfg.get("local_endpoint", "http://127.0.0.1:8080/v1")
        api_key = cfg.get("local_api_key") or "sable-local"
        return get_client(endpoint, api_key)

    if backend in _registry:
        return _registry[backend]

    if backend == "deepseek":
        from connectors.deepseek.client import get_client
        _registry[backend] = get_client()
    elif backend == "gemini":
        from connectors.gemini.client import get_client
        _registry[backend] = get_client()
    elif backend == "groq":
        from connectors.groq.client import get_client
        _registry[backend] = get_client()
    elif backend == "mistral":
        from connectors.mistral.client import get_client
        _registry[backend] = get_client()
    elif backend == "openai":
        from connectors.openai.client import get_client
        _registry[backend] = get_client()
    elif backend == "local":
        from connectors.local.client import get_client
        _registry[backend] = get_client()
    else:
        raise KeyError(f"No connector registered for api_backend='{backend}'")

    return _registry[backend]


def is_backend_available(backend: str) -> bool:
    """Check if a backend connector is ready (has valid creds, etc.)."""
    try:
        connector = get_connector(backend)
        return connector.is_available
    except Exception:
        return False
