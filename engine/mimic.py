"""Traffic mimicry for the curl fallback path.

The real Qwen web client does not just POST /chat/completions — within ~50ms
it also fires a couple of cheap authed GETs (chats/pinned, chats/?page=1), and
about 4s after the reply lands it fetches chats/<id>?direction=up to pull the
final message. Sable sends only the completions call, which is a visible
difference in request shape.

This module replays that *send burst* only. It deliberately does NOT replay:
  - the page-load sequence (once per boot, not per message)
  - POST /api/v2/users/status (Alibaba monitoring SDK, needs valid logId)
  - aplus.qwen.ai telemetry (third-party signed)
Faking those is more suspicious than omitting them.

Everything here is fire-and-forget: it never delays the stream, never raises,
and is a no-op unless SABLE_QWEN_MIMIC is set.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from typing import Any

logger = logging.getLogger("sable.mimic")

BASE = "https://chat.qwen.ai"


def mimic_enabled() -> bool:
    """Opt-in via SABLE_QWEN_MIMIC=1 (default off until measured)."""
    return os.environ.get("SABLE_QWEN_MIMIC", "0") == "1"


def _mimic_headers(headers: dict[str, str]) -> dict[str, str]:
    """Strip the streaming-only bits; these are plain XHRs, not SSE."""
    out = dict(headers)
    out.pop("X-Accel-Buffering", None)
    out.pop("x-accel-buffering", None)
    out["Accept"] = "application/json, text/plain, */*"
    return out


async def _get(client: Any, path: str, headers: dict[str, str]) -> None:
    try:
        await client.get(BASE + path, headers=headers, timeout=15)
    except Exception as exc:  # never propagate
        logger.debug("mimic GET %s failed: %s", path, exc)


async def post_send_burst(chat_id: str, headers: dict[str, str]) -> None:
    """Replay the client's send burst. Fire-and-forget; swallows all errors."""
    if not mimic_enabled() or not chat_id:
        return
    try:
        from curl_cffi.requests import AsyncSession
    except Exception:
        return

    h = _mimic_headers(headers)
    try:
        async with AsyncSession(impersonate="chrome150", timeout=20) as client:
            # 1) the two cheap list GETs, jittered like real network timing
            await asyncio.sleep(random.uniform(0.05, 0.20))
            await asyncio.gather(
                _get(client, "/api/v2/chats/pinned", h),
                _get(client, "/api/v2/chats/?page=1&exclude_project=true", h),
                return_exceptions=True,
            )
            # 2) ~4s later, the post-reply fetch — "I am reading my answer"
            await asyncio.sleep(random.uniform(3.5, 4.5))
            await _get(client, f"/api/v2/chats/{chat_id}?direction=up&limit=10", h)
    except Exception as exc:
        logger.debug("mimic burst failed: %s", exc)


def schedule_post_send_burst(chat_id: str, headers: dict[str, str]) -> None:
    """Schedule the burst on the running loop without awaiting it."""
    if not mimic_enabled() or not chat_id:
        return
    try:
        asyncio.get_running_loop().create_task(post_send_burst(chat_id, headers))
    except RuntimeError:
        # No running loop (sync caller) — silently skip.
        pass
