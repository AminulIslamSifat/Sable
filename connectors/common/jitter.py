"""Per-provider request jitter (anti-burst throttle).

DeepSeek / Qwen / z.ai / Gemini all throttle or ban accounts that fire requests
in tight bursts. This bites hardest during tool-calling loops, where EVERY tool
call + its output is sent upstream as a NEW user message — so a single turn can
produce a machine-gun sequence of requests at 500 tok/s and trip the WAF.

A short randomized gap between *genuine sends* breaks that burst pattern without
meaningfully slowing a single turn. Retries within one send do NOT get jitter —
that pacing is the provider's own retry delay.

Config lives in ``system/settings.json`` under ``request_jitter``::

    {
      "request_jitter": {
        "enabled": true,
        "min": 3.0,
        "max": 12.0,
        "providers": {"deepseek": true, "qwen": false, "zai": false, "gemini": false}
      }
    }

Defaults preserve prior behaviour: DeepSeek jitter ON (3-12s), other providers
OFF until explicitly enabled from Settings -> Providers.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from pathlib import Path
from typing import Any

logger = logging.getLogger("sable")

try:
    from engine.config import PERSISTENT_ROOT as _PROOT
    _SYSTEM_DIR = _PROOT / "system"
except Exception:  # pragma: no cover - fallback when engine not importable
    _SYSTEM_DIR = Path(__file__).resolve().parent.parent.parent / "system"

DEFAULT_MIN = 3.0
DEFAULT_MAX = 12.0

# Preserve existing behaviour: only DeepSeek shipped with jitter enabled.
_DEFAULT_PROVIDERS: dict[str, bool] = {
    "deepseek": True,
    "qwen": False,
    "zai": False,
    "gemini": False,
}

PROVIDERS = tuple(_DEFAULT_PROVIDERS.keys())


def get_jitter_config() -> dict[str, Any]:
    """Read the jitter config, applying defaults for anything missing."""
    raw: dict[str, Any] = {}
    path = _SYSTEM_DIR / "settings.json"
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            j = data.get("request_jitter")
            if isinstance(j, dict):
                raw = j
    except Exception as exc:
        logger.warning("jitter: failed to read settings: %s", exc)

    try:
        min_s = float(raw.get("min", DEFAULT_MIN))
    except (TypeError, ValueError):
        min_s = DEFAULT_MIN
    try:
        max_s = float(raw.get("max", DEFAULT_MAX))
    except (TypeError, ValueError):
        max_s = DEFAULT_MAX
    if min_s < 0:
        min_s = 0.0
    if max_s < 0:
        max_s = 0.0
    if max_s < min_s:
        min_s, max_s = max_s, min_s

    providers_raw = raw.get("providers")
    providers: dict[str, bool] = {}
    for prov, default in _DEFAULT_PROVIDERS.items():
        val = providers_raw.get(prov) if isinstance(providers_raw, dict) else None
        providers[prov] = bool(val) if val is not None else default

    return {
        "enabled": bool(raw.get("enabled", True)),
        "min": min_s,
        "max": max_s,
        "providers": providers,
    }


def jitter_for(provider: str) -> float:
    """Return the delay (seconds) to apply for a provider, or 0.0 if disabled."""
    cfg = get_jitter_config()
    if not cfg["enabled"]:
        return 0.0
    if not cfg["providers"].get(provider, False):
        return 0.0
    return random.uniform(cfg["min"], cfg["max"])


async def apply_jitter(provider: str) -> float | None:
    """Sleep a randomized anti-burst gap for a provider before a genuine send.

    Returns the delay applied in seconds, or ``None`` if jitter was skipped
    (disabled globally, disabled for this provider, or zero range). Callers may
    yield a status event so the UI can show why the turn is slow.
    """
    delay = jitter_for(provider)
    if delay <= 0:
        return None
    logger.debug("[%s] pre-request jitter: %.1fs", provider, delay)
    await asyncio.sleep(delay)
    return delay
