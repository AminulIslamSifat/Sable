"""Utility functions for the server."""

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Callable, Generator

from connectors.deepseek.client import get_client as get_deepseek_client
from engine.config import BROWSER_DATA_DIR, BROWSER_SCRAPER_DATA_DIR, BROWSER_AUTOMATION_DATA_DIR
from engine.scraper import get_settings as get_scraper_settings
from engine.memory_search import get_searcher, list_available_models

logger = logging.getLogger("sable")

MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0  # seconds; exponential backoff: 1s, 2s, 4s

SKILL_ROUND_WARN_THRESHOLD = 15

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "uploads"


def _dir_size_mb(path: Path) -> float:
    """Recursive dir size in MB (blocking — call via to_thread)."""
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
    """Extract the logged-in Google email from a Chromium profile's Preferences."""
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


def _build_conversation_summary(messages: list[dict[str, Any]], max_chars: int = 12000) -> str:
    """Build a compact conversation summary from messages for standalone consolidation."""
    parts: list[str] = []
    total = 0
    for msg in messages:
        role = msg.get("role", "unknown")
        content = str(msg.get("content", ""))
        # Truncate individual long messages
        if len(content) > 2000:
            content = content[:2000] + "...[truncated]"
        line = f"[{role}]: {content}"
        if total + len(line) > max_chars:
            parts.append(f"...[{len(messages) - messages.index(msg)} more messages truncated]")
            break
        parts.append(line)
        total += len(line)
    return "\n\n".join(parts)


async def retry_async(
    coro_factory: Callable[[], Any],
    *,
    max_retries: int = MAX_RETRIES,
    base_delay: float = RETRY_BASE_DELAY,
    label: str = "operation",
) -> Any:
    """Retry an async callable up to *max_retries* times with exponential backoff.

    ``coro_factory`` must be a zero-argument callable that returns a fresh
    awaitable each time it is called (so retries actually re-execute the work).
    On final failure the last exception is re-raised.
    """
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
    """Yield events from an async generator, retrying the whole stream on error."""
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            async for event in stream_factory():
                yield event
            return  # completed successfully
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


def sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def is_deepseek_api_model(model_id: str | None = None) -> bool:
    """True when the selected model routes through the DeepSeek HTTP API."""
    if not model_id:
        return False
    from engine.config import get_model_config
    cfg = get_model_config(model_id)
    if cfg.get("api_backend") != "deepseek":
        return False
    return get_deepseek_client().token is not None


# Account profile switcher — scans system/browser-data-acc* directories
_SYSTEM_DIR = BROWSER_DATA_DIR.parent  # system/
_ACTIVE_PROFILE_LINK = _SYSTEM_DIR / "browser-data"

_BROWSER_PROFILES: dict[str, tuple[Path, Path]] = {
    "api": (BROWSER_DATA_DIR, BROWSER_DATA_DIR.parent / (BROWSER_DATA_DIR.name + ".bak")),
    "scraper": (BROWSER_SCRAPER_DATA_DIR, BROWSER_SCRAPER_DATA_DIR.parent / (BROWSER_SCRAPER_DATA_DIR.name + ".bak")),
    "automation": (BROWSER_AUTOMATION_DATA_DIR, BROWSER_AUTOMATION_DATA_DIR.parent / (BROWSER_AUTOMATION_DATA_DIR.name + ".bak")),
}


def get_browser_profiles_dict() -> dict[str, tuple[Path, Path]]:
    return _BROWSER_PROFILES


def get_active_profile_link() -> Path:
    return _ACTIVE_PROFILE_LINK