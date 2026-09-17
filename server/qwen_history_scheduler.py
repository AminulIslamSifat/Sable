"""Qwen chat history auto-cleanup scheduler.

Runs a persistent asyncio loop that wakes up at 6:00 PM local time every day.
For each browser-data-accN profile, checks `last_cleared` in accounts.json.
If ≥5 days have elapsed (or never cleared), triggers deletion.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("sable.qwen_scheduler")

_SYSTEM_DIR = Path(__file__).resolve().parent.parent / "system"
_CLEAR_INTERVAL_DAYS = 5
_FIRE_HOUR = 18  # 6:00 PM
_FIRE_MINUTE = 0

_task: asyncio.Task | None = None


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _next_fire_time(after: datetime | None = None) -> datetime:
    """Compute next 6:00 PM local time."""
    now = after or _local_now()
    candidate = now.replace(hour=_FIRE_HOUR, minute=_FIRE_MINUTE, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def _needs_clear(profile_name: str) -> bool:
    """Check if an account needs clearing (never cleared or ≥5 days ago)."""
    from engine.qwen_cleanup import get_last_cleared
    last = get_last_cleared(profile_name)
    if last is None:
        return True
    # Compare in UTC
    now_utc = datetime.now(timezone.utc)
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now_utc - last).days >= _CLEAR_INTERVAL_DAYS


def _get_all_profiles() -> list[str]:
    """Return sorted list of browser-data-accN directory names."""
    profiles = []
    for p in _SYSTEM_DIR.iterdir():
        if p.is_dir() and p.name.startswith("browser-data-acc"):
            suffix = p.name.split("acc", 1)[1]
            if suffix.isdigit():
                profiles.append(p.name)
    profiles.sort(key=lambda x: int(x.split("acc")[1]))
    return profiles


async def _run_scheduled_clear() -> None:
    """Execute the scheduled clear for all due accounts."""
    from engine.qwen_cleanup import delete_all_chats_for_profile

    profiles = _get_all_profiles()
    due = [p for p in profiles if _needs_clear(p)]

    if not due:
        logger.info("No accounts due for Qwen history cleanup today")
        return

    logger.info("Qwen history cleanup: %d/%d accounts due", len(due), len(profiles))

    for profile in due:
        try:
            result = await delete_all_chats_for_profile(profile)
            if result["success"]:
                logger.info("✅ Auto-cleared %s", profile)
            else:
                logger.warning("❌ Auto-clear failed for %s: %s", profile, result.get("error"))
        except Exception as e:
            logger.error("Exception clearing %s: %s", profile, e)
        # Small delay between accounts to avoid hammering
        await asyncio.sleep(2)


async def _scheduler_loop() -> None:
    """Main loop: sleep until 6 PM, run clear, repeat."""
    logger.info("Qwen history scheduler started (fires daily at %02d:%02d, interval=%dd)",
                _FIRE_HOUR, _FIRE_MINUTE, _CLEAR_INTERVAL_DAYS)

    while True:
        next_fire = _next_fire_time()
        now = _local_now()
        delay = max(0, (next_fire - now).total_seconds())

        logger.info("Next Qwen history cleanup check at %s (in %.0fs)", next_fire.isoformat(), delay)
        await asyncio.sleep(delay)

        try:
            await _run_scheduled_clear()
        except Exception as e:
            logger.error("Scheduled Qwen clear run failed: %s", e)

        # Buffer to avoid re-firing same minute
        await asyncio.sleep(60)


def start_qwen_history_scheduler() -> None:
    """Start the background scheduler. Safe to call multiple times."""
    global _task
    if _task is not None and not _task.done():
        return
    loop = asyncio.get_running_loop()
    _task = loop.create_task(_scheduler_loop(), name="qwen-history-scheduler")
    logger.info("Qwen history scheduler task created")


def stop_qwen_history_scheduler() -> None:
    """Cancel the scheduler."""
    global _task
    if _task and not _task.done():
        _task.cancel()
        _task = None
        logger.info("Qwen history scheduler stopped")
