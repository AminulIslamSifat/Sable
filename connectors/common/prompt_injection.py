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
PROVIDER_LABELS: dict[str, str] = {
    "deepseek": "DeepSeek (API)",
    "qwen": "Qwen",
    "gemini": "Gemini (API)",
    "gemini_web": "Gemini (Web)",
    "zai": "z.ai",
    "kimi": "Kimi",
    "mimo": "MiMo",
    "mistral_web": "Mistral (Web)",
    "chatgpt": "ChatGPT (Web)",
    "native": "OpenAI-compat / Custom",
    "local": "Local (llama-server)",
    "scraping": "Scraper (generic)",
}

PROVIDERS: tuple[str, ...] = tuple(PROVIDER_LABELS.keys())

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
    for prov in PROVIDERS:
        row = providers_raw.get(prov)
        cfg = _defaults()
        if isinstance(row, dict):
            for k in INJECTION_KEYS:
                if k in row:
                    cfg[k] = bool(row[k])
        providers[prov] = cfg

    return {"providers": providers}


def injection_enabled_map(provider: str | None) -> dict[str, bool]:
    """Return the full ``key -> bool`` map for a provider.

    Unknown providers fall back to all-on so a new connector is never
    silently starved of context.
    """
    prov = canonical_provider(provider)
    cfg = get_injection_config()["providers"]
    row = cfg.get(prov)
    if row is None:
        return _defaults()
    return dict(row)


def is_enabled(provider: str | None, key: str) -> bool:
    """True if the ``key`` fragment should be injected for ``provider``."""
    if key not in INJECTION_KEYS:
        return True
    return bool(injection_enabled_map(provider).get(key, True))
