"""Per-provider prompt injection toggles.

Controls which system-prompt and message-prefix fragments Sable injects for
a given provider. Disabling a key means the fragment is NEVER added — the
model sees none of it, not even a stub.

Two families of keys:

* **System-prompt keys** (assembled in ``instruction_builder.build_instructions``):
  ``persona``, ``output_format``, ``skills``, ``tools``, ``tool_format``,
  ``mcp``, ``output_dir``, ``facts``, ``git``
* **Message-prefix keys** (assembled in ``server/api/routes/chat.py``):
  ``chat_title``, ``platform``, ``schedule``, ``timestamp``, ``memory``

Config lives in ``system/settings.json`` under ``prompt_injection``::

    {
      "prompt_injection": {
        "providers": {
          "deepseek": {"persona": true, "tools": false, ...},
          "qwen":     {"persona": true, ...}
        }
      }
    }

Defaults: every key ON for every provider, so behaviour is unchanged until
something is explicitly toggled off from Settings -> Providers.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

try:
    from engine.config import PERSISTENT_ROOT as _PROOT
    _SYSTEM_DIR = _PROOT / "system"
except Exception:  # pragma: no cover - fallback when engine not importable
    _SYSTEM_DIR = Path(__file__).resolve().parent.parent.parent / "system"

logger = logging.getLogger("sable")


# All toggleable injection keys, in UI display order.
INJECTION_KEYS: tuple[str, ...] = (
    "persona",
    "output_format",
    "skills",
    "tools",
    "tool_format",
    "mcp",
    "output_dir",
    "facts",
    "git",
    "chat_title",
    "platform",
    "schedule",
    "timestamp",
    "memory",
)

KEY_LABELS: dict[str, str] = {
    "persona": "Persona (Maria.md + personal.md)",
    "output_format": "Output format rules",
    "skills": "Skill registry",
    "tools": "Tool schemas",
    "tool_format": "Tool-call format primer",
    "mcp": "MCP tools",
    "output_dir": "Output directory mandate",
    "facts": "Project facts",
    "git": "Git details",
    "chat_title": "chat_title nudge",
    "platform": "Platform hint",
    "schedule": "Schedule context",
    "timestamp": "Timestamp + cwd prefix",
    "memory": "Relevant memory context",
}

# Canonical provider keys shown in the UI.
# This is the *static* set — real user-added endpoints are appended at runtime
# by ``list_providers()`` below, keyed by endpoint URL rather than by backend
# (every custom endpoint resolves to api_backend="local", so keying on the
# backend alone would merge them all into one row).
PROVIDER_LABELS: dict[str, str] = {
    "deepseek": "DeepSeek (API)",
    "qwen": "Qwen",
    "gemini": "Gemini (API)",
    "gemini_web": "Gemini (Web)",
    "zai": "z.ai",
    "kimi": "Kimi",
    "mimo": "MiMo",
    "mistral": "Mistral (API)",
    "mistral_web": "Mistral (Web)",
    "groq": "Groq",
    "chatgpt": "ChatGPT (Web)",
    "native": "OpenAI-compat / Custom",
    "local": "Local (llama-server)",
    "scraping": "Scraper (generic)",
}

PROVIDERS: tuple[str, ...] = tuple(PROVIDER_LABELS.keys())

# Prefix for dynamically-discovered custom endpoints.
# A config key of ``ep:<url>`` targets one specific endpoint; the bare backend
# key (``local``) stays as the catch-all fallback for endpoints with no entry.
ENDPOINT_PREFIX = "ep:"


def endpoint_key(url: str | None) -> str:
    """Canonical config key for a custom endpoint URL."""
    if not url:
        return ""
    return ENDPOINT_PREFIX + str(url).strip().rstrip("/")


def list_custom_endpoints() -> list[dict[str, str]]:
    """Discover user-added endpoints from the custom model registry.

    Groups by endpoint URL so two models on the same base URL share one row.
    Returns ``[{key, label, url}]`` sorted by label.
    """
    out: dict[str, dict[str, str]] = {}
    try:
        from engine.config import get_all_models
        for m in get_all_models():
            if str(m.get("api_backend") or "") != "local":
                continue
            url = str(m.get("local_endpoint") or "").strip()
            if not url:
                continue
            key = endpoint_key(url)
            if not key:
                continue
            # Strip the emoji/feature suffix the picker appends, e.g. "Glm 4.7 Flash 🧠🔧"
            label = str(m.get("label") or "").strip() or url
            label = label.split(" 🧠")[0].split(" 🔧")[0].strip() or url
            host = _host_of(url) or url
            if key not in out:
                out[key] = {"key": key, "label": label, "url": url, "host": host, "models": []}
            if label not in out[key]["models"]:
                out[key]["models"].append(label)
    except Exception as exc:
        logger.warning("prompt_injection: endpoint discovery failed: %s", exc)

    # When one endpoint backs several models, say so rather than showing just
    # the first label — otherwise "Gpt Oss 120B" silently also governs Glm.
    for row in out.values():
        if len(row["models"]) > 1:
            row["label"] = f"{row['host']} ({len(row['models'])} models)"
    return sorted(out.values(), key=lambda d: d["label"].lower())


def _host_of(url: str) -> str:
    """Bare hostname for a URL, for compact endpoint labels."""
    try:
        from urllib.parse import urlparse
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def list_providers() -> tuple[list[dict[str, str]], dict[str, str]]:
    """Return (ordered provider rows, key->label map) including custom endpoints.

    Each row is ``{key, label, group}``. Static backends come first, then
    user-added endpoints under a separate group so the picker can show them
    as "Custom Endpoints".
    """
    rows: list[dict[str, str]] = []
    labels: dict[str, str] = {}
    for k, v in PROVIDER_LABELS.items():
        rows.append({"key": k, "label": v, "group": "Backends"})
        labels[k] = v
    for ep in list_custom_endpoints():
        rows.append({
            "key": ep["key"],
            "label": ep["label"],
            "group": "Custom Endpoints",
            "models": list(ep.get("models", [])),
            "url": ep.get("url", ""),
        })
        labels[ep["key"]] = ep["label"]
    return rows, labels

# Raw provider strings that connectors actually pass -> canonical UI key.
# The Gemini API client passes provider="none" (it only cares about the tool
# format being native), and the OpenAI-compat / Cloudflare path passes
# "native" — both must map onto a real, selectable row.
_ALIASES: dict[str, str] = {
    "none": "gemini",
    "gemini_api": "gemini",
    "openai_compat": "native",
    "cloudflare": "native",
    "url": "native",
    "deepseek_scraper": "deepseek",
    "qwen_scraper": "qwen",
}


def canonical_provider(provider: str | None) -> str:
    """Normalise a raw connector provider string to a canonical UI key."""
    if not provider:
        return ""
    p = str(provider).strip().lower()
    return _ALIASES.get(p, p)


def _defaults() -> dict[str, bool]:
    return {k: True for k in INJECTION_KEYS}


def get_injection_config() -> dict[str, Any]:
    """Read the prompt-injection config, filling defaults for anything missing."""
    raw: dict[str, Any] = {}
    path = _SYSTEM_DIR / "settings.json"
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            pi = data.get("prompt_injection")
            if isinstance(pi, dict):
                raw = pi
    except Exception as exc:
        logger.warning("prompt_injection: failed to read settings: %s", exc)

    providers_raw = raw.get("providers")
    if not isinstance(providers_raw, dict):
        providers_raw = {}

    providers: dict[str, dict[str, bool]] = {}
    # Static backends always get a row.
    for prov in PROVIDERS:
        providers[prov] = _row_from(providers_raw.get(prov))
    # Preserve any dynamically-discovered endpoint rows already saved.
    for prov, row in providers_raw.items():
        if prov in providers:
            continue
        if str(prov).startswith(ENDPOINT_PREFIX):
            providers[prov] = _row_from(row)

    return {"providers": providers}


def _row_from(row: Any) -> dict[str, bool]:
    cfg = _defaults()
    if isinstance(row, dict):
        for k in INJECTION_KEYS:
            if k in row:
                cfg[k] = bool(row[k])
    return cfg


def injection_enabled_map(provider: str | None) -> dict[str, bool]:
    """Return the full ``key -> bool`` map for a provider.

    Resolution order for a custom endpoint (``api_backend="local"`` with a
    ``local_endpoint`` URL):

    1. an explicit ``ep:<url>`` row, if the user configured one
    2. the bare backend row (``local``) as the catch-all fallback
    3. all-on defaults

    Unknown providers fall back to all-on so a new connector is never
    silently starved of context.
    """
    prov = canonical_provider(provider)
    cfg = get_injection_config()["providers"]
    row = cfg.get(prov)
    if row is not None:
        return dict(row)
    # Endpoint key not configured yet -> inherit its backend's row.
    if prov.startswith(ENDPOINT_PREFIX):
        base = cfg.get(prov[len(ENDPOINT_PREFIX):].split(":")[0]) or cfg.get("local")
        if base is not None:
            return dict(base)
    return _defaults()


def is_enabled(provider: str | None, key: str) -> bool:
    """True if the ``key`` fragment should be injected for ``provider``."""
    if key not in INJECTION_KEYS:
        return True
    return bool(injection_enabled_map(provider).get(key, True))
