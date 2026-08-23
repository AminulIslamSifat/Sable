
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

# Remote custom-endpoint clients (unified OpenAI-compat), keyed by endpoint URL.
# Cached so session history persists across requests for the same endpoint.
_remote_clients: dict[str, Any] = {}


def _is_remote_endpoint(endpoint: str) -> bool:
    """True if the endpoint is a remote (non-localhost) OpenAI-compat API."""
    from urllib.parse import urlparse
    try:
        p = urlparse(endpoint)
    except Exception:
        return False
    if p.scheme == "https":
        return True
    host = (p.hostname or "").lower()
    return host not in ("localhost", "127.0.0.1", "0.0.0.0", "::1", "")


def resolve_backend(model_id: str | None) -> str | None:
    """Return the api_backend string for a model, or None if it's a Qwen/scraper model."""
    if not model_id:
        return None
    cfg = get_model_config(model_id)
    return cfg.get("api_backend")  # None for Qwen models


def get_connector(backend: str, model_id: str | None = None) -> Any:
    """Get (or lazily create) the connector instance for a backend name.

    For 'local' backend, pass model_id to resolve the correct endpoint.
    Remote OpenAI-compat endpoints (Cloudflare, etc.) use the unified
    OpenAICompatClient; localhost endpoints keep the llama-server connector.
    Raises KeyError if the backend has no registered connector.
    """
    # Local backend needs per-model endpoint resolution
    if backend == "local" and model_id:
        cfg = get_model_config(model_id)
        endpoint = cfg.get("local_endpoint", "http://127.0.0.1:8080/v1")
        api_key = cfg.get("local_api_key") or "sable-local"
        # Remote OpenAI-compat endpoint -> unified client (session-aware)
        if _is_remote_endpoint(endpoint):
            if endpoint not in _remote_clients:
                from connectors.common.openai_compat import OpenAICompatClient
                _remote_clients[endpoint] = OpenAICompatClient(
                    base_url=endpoint,
                    api_key=api_key,
                    name=cfg.get("label", "Custom").lstrip("☁️ ").strip() or "Custom",
                    default_model=cfg.get("api_model_type", ""),
                    instruction_mode=None,
                )
            return _remote_clients[endpoint]
        # Localhost (llama-server etc.) -> specialized local connector
        from connectors.local.client import get_client
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
    elif backend == "cloudflare":
        # Cloudflare Workers AI text models — OpenAI-compat endpoint.
        # Each model needs its own client because api_model_type differs
        # (e.g. @cf/openai/gpt-oss-120b vs @cf/meta/llama-4-scout).
        # Cached by (endpoint, model_id) so session history still persists.
        if model_id:
            cfg = get_model_config(model_id)
            endpoint = cfg.get("local_endpoint", "")
            api_key = cfg.get("local_api_key", "")
            api_model = cfg.get("api_model_type", "")
            cache_key = f"{endpoint}::{model_id}"
            if endpoint and cache_key not in _remote_clients:
                from connectors.common.openai_compat import OpenAICompatClient
                client = OpenAICompatClient(
                    base_url=endpoint,
                    api_key=api_key,
                    name=cfg.get("label", "Cloudflare").lstrip("☁️ ").strip() or "Cloudflare",
                    default_model=api_model,
                    instruction_mode=None,
                )
                client.SUPPORTS_MULTIMODAL_CONTENT = False
                _remote_clients[cache_key] = client
            if cache_key in _remote_clients:
                return _remote_clients[cache_key]
        raise KeyError(f"Cloudflare backend requires model_id with local_endpoint configured")
    else:
        raise KeyError(f"No connector registered for api_backend='{backend}'")

    return _registry[backend]


def is_backend_available(backend: str) -> bool:
    """Check if a backend connector is ready (has valid creds, etc.)."""
    if backend == "cloudflare":
        # Cloudflare needs model_id to create a client; check if any CF model has a valid endpoint
        from engine.config import get_all_models
        for cfg in get_all_models():
            if cfg.get("api_backend") == "cloudflare" and cfg.get("local_endpoint"):
                return True
        return False
    try:
        connector = get_connector(backend)
        return connector.is_available
    except Exception:
        return False
#
