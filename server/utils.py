from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from collections.abc import AsyncGenerator

from engine.config import get_model_config
from connectors.deepseek.client import get_client as get_deepseek_client
from .config import MAX_RETRIES, RETRY_BASE_DELAY
from .logging_setup import logger   # <-- added re‑export

def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()

async def retry_async(
    coro_factory: Callable[[], Any],
    *,
    max_retries: int = MAX_RETRIES,
    base_delay: float = RETRY_BASE_DELAY,
    label: str = "operation",
) -> Any:
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return await coro_factory()
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.warning(
                    "%s failed (attempt %d/%d): %s — retrying in %.1fs",
                    label, attempt + 1, max_retries + 1, exc, delay,
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "%s failed permanently after %d attempts: %s",
                    label, max_retries + 1, exc,
                )
    raise last_exc  # type: ignore[misc]

async def retry_stream(
    stream_factory: Callable[[], AsyncGenerator[dict[str, Any], None]],
    *,
    max_retries: int = MAX_RETRIES,
    base_delay: float = RETRY_BASE_DELAY,
    label: str = "stream",
) -> AsyncGenerator[dict[str, Any], None]:
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            async for event in stream_factory():
                yield event
            return
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.warning(
                    "%s failed (attempt %d/%d): %s — retrying in %.1fs",
                    label, attempt + 1, max_retries + 1, exc, delay,
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "%s failed permanently after %d attempts: %s",
                    label, max_retries + 1, exc,
                )
    if last_exc:
        raise last_exc

def make_title(message: str) -> str:
    clean = " ".join(message.split())
    return clean[:48] or "New chat"

def _build_conversation_summary(messages: list[dict[str, Any]], max_chars: int = 12000) -> str:
    parts: list[str] = []
    total = 0
    for msg in messages:
        role = msg.get("role", "unknown")
        content = str(msg.get("content", ""))
        if len(content) > 2000:
            content = content[:2000] + "...[truncated]"
        line = f"[{role}]: {content}"
        if total + len(line) > max_chars:
            parts.append(f"...[{len(messages) - messages.index(msg)} more messages truncated]")
            break
        parts.append(line)
        total += len(line)
    return "\n\n".join(parts)

def _is_deepseek_api_model(model_id: str | None = None) -> bool:
    if not model_id:
        return False
    cfg = get_model_config(model_id)
    if cfg.get("api_backend") != "deepseek":
        return False
    return get_deepseek_client().token is not None

def _dir_size_mb(path: Path) -> float:
    if not path.is_dir():
        return 0.0
    total = 0
    for f in path.rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return round(total / 1_048_576, 1)

def _read_profile_email(profile_dir: Path) -> str | None:
    prefs_file = profile_dir / "Default" / "Preferences"
    if not prefs_file.exists():
        return None
    try:
        prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
        accounts = prefs.get("account_info", [])
        if accounts:
            return accounts[0].get("email")
    except Exception:
        pass
    return None