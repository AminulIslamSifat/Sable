"""OpenCode free-model plugin.

On every Sable start, fetch ``https://models.opencode.ai/api.json``, take every
model under provider ``opencode`` that is (a) zero-cost and (b) not marked
``deprecated``, and register them as selectable models backed by the
``opencode`` connector.

Capabilities and thinking modes come from the catalog's own fields:

    reasoning: true + reasoning_options  -> thinking_modes
        {"type": "toggle"}                    -> Fast / Thinking
        {"type": "effort", "values": [...]}  -> one mode per effort level
    tool_call: true                      -> capabilities.tool_call
    modalities.input                     -> capabilities.image/video/document/audio
    limit.context                        -> max_session_chars

No binary involved. The ``opencode`` CLI reads this same catalog.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("sable.plugins.opencode_free")

_CATALOG = "https://models.opencode.ai/api.json"
_PROVIDER = "opencode"
_ID_PREFIX = "oc-"
_BACKEND = "opencode"

# The OpenCode gateway exposes several wire protocols under one host:
#   /inference/openai/v1      -> OpenAI chat-completions  (what we speak)
#   /inference/anthropic/v1   -> Anthropic messages
#   /inference/google/v1beta  -> Google generateContent
#   /inference/systemone/v1   -> internal, decision/classifier models
# Only the first is compatible with this connector.
_OPENAI_API_SUFFIX = "/inference/openai/v1"

# Non-chat model kinds that must never enter the picker.
_NON_CHAT_TYPES = frozenset({"decision", "embedding", "image", "audio", "rerank"})


def _is_free(meta: dict[str, Any]) -> bool:
    """Zero input cost across every tier."""
    cost = meta.get("cost")
    tiers = cost if isinstance(cost, list) else ([cost] if cost else [])
    inputs = [(t or {}).get("input", 0) or 0 for t in tiers] or [0]
    return max(inputs) == 0


def _is_live(meta: dict[str, Any]) -> bool:
    return (meta.get("status") or "") != "deprecated"


def _speaks_openai(meta: dict[str, Any]) -> bool:
    """True when the model is served over OpenAI chat-completions.

    models.dev lists every protocol the gateway speaks; a model on
    ``/anthropic/v1`` or ``/systemone/v1`` would fail on every request from
    this connector, so it is filtered out at registration time.
    """
    api = ((meta.get("provider") or {}).get("api") or "")
    return api.rstrip("/").endswith(_OPENAI_API_SUFFIX)


def _is_chat_model(meta: dict[str, Any]) -> bool:
    return (meta.get("type") or "") not in _NON_CHAT_TYPES


def _thinking_modes(meta: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate ``reasoning_options`` into Sable's thinking-mode list.

    The catalog advertises a *union* of accepted shapes, not separate knobs:
    a model accepts either a plain toggle or an explicit effort level, never
    both. So when effort values are present they replace the toggle entirely —
    listing "thinking" next to "low/high/max" would offer the same setting twice.
    """
    fast = {
        "id": "fast",
        "label": "Fast",
        "thinking_enabled": False,
        "auto_thinking": False,
        "thinking_mode": "Fast",
    }
    if not meta.get("reasoning"):
        return [fast]

    modes: list[dict[str, Any]] = [fast]
    seen = {"fast"}

    # Effort levels win over the toggle when both are advertised.
    efforts: list[str] = []
    has_toggle = False
    for option in meta.get("reasoning_options") or []:
        kind = (option or {}).get("type")
        if kind == "toggle":
            has_toggle = True
        elif kind == "effort":
            for value in option.get("values") or []:
                if value and value != "none" and value not in efforts:
                    efforts.append(value)

    if efforts:
        for value in efforts:
            if value in seen:
                continue
            modes.append(
                {
                    "id": value,
                    "label": value.capitalize(),
                    "thinking_enabled": True,
                    "auto_thinking": False,
                    "thinking_mode": value,
                }
            )
            seen.add(value)
        return modes

    if has_toggle:
        modes.append(
            {
                "id": "thinking",
                "label": "Thinking",
                "thinking_enabled": True,
                "auto_thinking": False,
                "thinking_mode": "Thinking",
            }
        )
    return modes


def _capabilities(meta: dict[str, Any]) -> dict[str, bool]:
    inputs = set(((meta.get("modalities") or {}).get("input")) or [])
    return {
        "image": "image" in inputs,
        "video": "video" in inputs,
        "document": bool(inputs & {"pdf", "document"}),
        "audio": "audio" in inputs,
        "tool_call": bool(meta.get("tool_call")),
        "structured_output": bool(meta.get("structured_output")),
    }


def _to_model_def(raw_id: str, meta: dict[str, Any]) -> dict[str, Any]:
    limit = meta.get("limit") or {}
    context = limit.get("context") or 200_000
    return {
        "id": _ID_PREFIX + raw_id,
        "label": meta.get("name") or raw_id,
        "api_backend": _BACKEND,
        "api_model_type": raw_id,
        "max_session_chars": int(context) * 4,
        "capabilities": _capabilities(meta),
        "thinking_modes": _thinking_modes(meta),
    }


async def fetch_free_models() -> list[dict[str, Any]]:
    """Pull the catalog and return live free opencode models as Sable defs."""
    async with httpx.AsyncClient(timeout=25) as client:
        resp = await client.get(_CATALOG)
        resp.raise_for_status()
        catalog = resp.json()

    provider = catalog.get(_PROVIDER) or {}
    models = provider.get("models") or {}
    return [
        _to_model_def(raw_id, meta)
        for raw_id, meta in models.items()
        if _is_free(meta) and _is_live(meta) and _speaks_openai(meta) and _is_chat_model(meta)
    ]


async def sync() -> dict[str, Any]:
    """Add, update, and prune free opencode models in the custom registry."""
    from engine.config import (
        _load_custom_models,
        _load_hidden_models,
        add_custom_model,
        remove_custom_model,
    )

    try:
        wanted = await fetch_free_models()
    except Exception as exc:
        logger.warning("opencode_free: catalog fetch failed: %s", exc)
        return {"ok": False, "error": str(exc)}

    hidden = set(_load_hidden_models())
    wanted = [m for m in wanted if m["id"] not in hidden]
    wanted_ids = {m["id"] for m in wanted}

    existing = {
        m["id"]: m
        for m in _load_custom_models()
        if m.get("api_backend") == _BACKEND
    }

    added = updated = removed = 0
    for model in wanted:
        prev = existing.get(model["id"])
        if prev is None:
            add_custom_model(model)
            added += 1
        elif prev != model:
            add_custom_model(model)
            updated += 1

    for model_id in existing:
        if model_id not in wanted_ids:
            remove_custom_model(model_id)
            removed += 1

    logger.info(
        "opencode_free: %d added, %d updated, %d removed (%d live free models)",
        added,
        updated,
        removed,
        len(wanted),
    )
    return {
        "ok": True,
        "added": added,
        "updated": updated,
        "removed": removed,
        "total": len(wanted),
    }
