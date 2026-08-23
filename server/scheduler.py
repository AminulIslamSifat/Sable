
"""Event-driven agent ops scheduler.

Each enabled agent op gets its own asyncio.Task that sleeps until next_run,
fires the agent, computes the next occurrence, and reschedules itself.
No polling. No wasted cycles.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any

logger = logging.getLogger("sable.scheduler")

# Active scheduled tasks keyed by agent_op id
_scheduled_tasks: dict[str, asyncio.Task] = {}
_loop: asyncio.AbstractEventLoop | None = None


# ── Skill configuration for scheduled agents ────────────────────────────────

AGENT_OP_SKILLS = {
    # Full instruction.md auto-injected into prompt
    "default_skills": ["code_editor", "online_search"],
    # All skills the agent CAN use (extra ones get compact registry listing)
    "allowed_skills": [
        "execute_command",   # universal, always available
        "code_editor",       # default — full instruction loaded
        "online_search",     # default — full instruction loaded
        "file_uploader",     # allowed — compact listing (get_file lives here)
        "telegram",          # allowed — compact listing, loads on demand
        "email",             # allowed — compact listing, loads on demand
    ],
}


# ── Next-run computation ────────────────────────────────────────────────────

# Use system local timezone so schedule_time matches the user's wall clock.
_LOCAL_TZ = datetime.now().astimezone().tzinfo


def _local_now() -> datetime:
    """Current time in system local timezone."""
    return datetime.now(_LOCAL_TZ)


def compute_next_run(
    schedule_type: str,
    schedule_time: str | None,
    schedule_day: int | None,
    cron_expression: str | None,
    after: datetime | None = None,
) -> datetime | None:
    """Compute the next local-time datetime this op should fire.

    schedule_time is interpreted in the user's local timezone (system tz),
    NOT UTC. This matches what users see on their clock.
    """
    now = after or _local_now()
    # Ensure now is tz-aware in local tz
    if now.tzinfo is None:
        now = now.replace(tzinfo=_LOCAL_TZ)

    if schedule_type == "daily" and schedule_time:
        h, m = map(int, schedule_time.split(":"))
        candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    if schedule_type == "weekly" and schedule_time is not None and schedule_day is not None:
        h, m = map(int, schedule_time.split(":"))
        # schedule_day: 0=Mon ... 6=Sun (Python weekday: Mon=0)
        days_ahead = (schedule_day - now.weekday()) % 7
        candidate = now.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=days_ahead)
        if candidate <= now:
            candidate += timedelta(days=7)
        return candidate

    if schedule_type == "cron" and cron_expression:
        try:
            from croniter import croniter
            cron = croniter(cron_expression, now)
            nxt = cron.get_next(datetime)
            if nxt.tzinfo is None:
                nxt = nxt.replace(tzinfo=_LOCAL_TZ)
            return nxt
        except Exception as exc:
            logger.warning("Cron parse failed for '%s': %s", cron_expression, exc)
            return None

    return None


# ── Agent firing ────────────────────────────────────────────────────────────

async def _fire_agent_op(op: dict[str, Any]) -> None:
    """Spawn an agent for a scheduled op and update DB state."""
    from server.database import update_agent_op
    from server.utils import utcnow
    from engine.agents import get_runtime, TaskAssignment
    from engine.agents.registry import get_role_config, get_next_account
    from engine.config import _SYSTEM as _AGENT_SYSTEM_DIR

    op_id = op["id"]
    role = "scheduled"
    logger.info("Firing agent op '%s' (%s)", op["name"], op_id)

    runtime = get_runtime()
    role_cfg = get_role_config(role)

    # Resolve browser_data_dir via round-robin account pool (same as normal spawn)
    browser_data = None
    assigned_account = get_next_account(role)
    if assigned_account:
        acct_profile = _AGENT_SYSTEM_DIR / assigned_account
        if acct_profile.is_dir():
            browser_data = str(acct_profile)

    assignment = TaskAssignment(
        task=op["prompt"],
        role=role,
        model=op.get("model", role_cfg.default_model),
        timeout=role_cfg.default_timeout,
        browser_data_dir=browser_data,
    )

    # We need a chat_id for the agent. Use a dedicated scheduled-ops chat.
    from server.database import ensure_chat
    sched_chat_id = "__scheduled_ops__"
    ensure_chat(sched_chat_id, title="Scheduled Agent Ops")

    try:
        agent = await runtime.spawn(assignment, sched_chat_id)
        last_run = utcnow()
        next_run = compute_next_run(
            op.get("schedule_type", "daily"),
            op.get("schedule_time"),
            op.get("schedule_day"),
            op.get("cron_expression"),
        )
        next_run_iso = next_run.isoformat() if next_run else None
        update_agent_op(op_id, last_run=last_run, next_run=next_run_iso)
        logger.info("Agent op '%s' fired successfully. Next run: %s", op["name"], next_run_iso)
    except Exception as exc:
        logger.error("Agent op '%s' failed to fire: %s", op["name"], exc)
        # Still reschedule so it doesn't die permanently
        next_run = compute_next_run(
            op.get("schedule_type", "daily"),
            op.get("schedule_time"),
            op.get("schedule_day"),
            op.get("cron_expression"),
        )
        next_run_iso = next_run.isoformat() if next_run else None
        update_agent_op(op_id, last_run=utcnow(), next_run=next_run_iso,
                        last_result=f"FAILED: {exc}")


# ── Per-op scheduling loop ──────────────────────────────────────────────────

async def _op_sleep_loop(op_id: str) -> None:
    """Sleep until next_run, fire, reschedule. Runs forever until cancelled."""
    from server.database import get_agent_op

    while True:
        op = get_agent_op(op_id)
        if not op or not op.get("enabled"):
            logger.debug("Op '%s' disabled or deleted — stopping loop", op_id)
            return

        next_run_str = op.get("next_run")
        if not next_run_str:
            # Compute and persist next_run
            nr = compute_next_run(
                op.get("schedule_type", "daily"),
                op.get("schedule_time"),
                op.get("schedule_day"),
                op.get("cron_expression"),
            )
            if not nr:
                logger.warning("Cannot compute next_run for op '%s' — stopping", op_id)
                return
            from server.database import update_agent_op
            update_agent_op(op_id, next_run=nr.isoformat())
            next_run_str = nr.isoformat()

        try:
            next_run_dt = datetime.fromisoformat(next_run_str)
            if next_run_dt.tzinfo is None:
                # Legacy entries stored without tz — assume local
                next_run_dt = next_run_dt.replace(tzinfo=_LOCAL_TZ)
        except (ValueError, TypeError):
            logger.warning("Invalid next_run '%s' for op '%s'", next_run_str, op_id)
            return

        now = _local_now()
        delay = max(0, (next_run_dt - now).total_seconds())

        if delay > 0:
            logger.debug("Op '%s' sleeping %.0fs until %s", op_id, delay, next_run_str)
            await asyncio.sleep(delay)

        # Fire!
        try:
            await _fire_agent_op(op)
        except Exception as exc:
            logger.error("Unhandled error firing op '%s': %s", op_id, exc)

        # Small buffer to avoid re-firing same second
        await asyncio.sleep(1)


# ── Public API ──────────────────────────────────────────────────────────────

def schedule_op(op_id: str) -> None:
    """Schedule (or reschedule) an agent op. Safe to call multiple times."""
    global _loop
    if _loop is None:
        logger.warning("Scheduler not started — cannot schedule op '%s'", op_id)
        return

    # Cancel existing task if any
    cancel_op(op_id)

    task = _loop.create_task(_op_sleep_loop(op_id), name=f"sched-{op_id}")
    _scheduled_tasks[op_id] = task
    logger.info("Scheduled agent op '%s'", op_id)


def cancel_op(op_id: str) -> None:
    """Cancel a scheduled agent op task."""
    task = _scheduled_tasks.pop(op_id, None)
    if task and not task.done():
        task.cancel()
        logger.debug("Cancelled agent op '%s'", op_id)


def cancel_all() -> None:
    """Cancel all scheduled ops (shutdown)."""
    for op_id in list(_scheduled_tasks.keys()):
        cancel_op(op_id)
    logger.info("All scheduled ops cancelled")


async def start_scheduler() -> None:
    """Load all enabled ops and schedule them. Called once at startup."""
    global _loop
    _loop = asyncio.get_running_loop()

    from server.database import list_agent_ops, update_agent_op

    ops = list_agent_ops()
    count = 0
    for op in ops:
        if not op.get("enabled"):
            continue
        # Always recompute next_run on startup — stored value may be stale
        # (e.g. after timezone fix, clock change, or missed runs)
        nr = compute_next_run(
            op.get("schedule_type", "daily"),
            op.get("schedule_time"),
            op.get("schedule_day"),
            op.get("cron_expression"),
        )
        if nr:
            update_agent_op(op["id"], next_run=nr.isoformat())
        schedule_op(op["id"])
        count += 1

    logger.info("Scheduler started with %d active agent op(s)", count)


# ── Skill injection hook ───────────────────────────────────────────────────

def get_agent_op_skill_config() -> dict[str, list[str]]:
    """Return skill config for scheduled agent ops.

    Returns dict with 'allowed_skills' and 'default_skills' keys.
    Used when building custom RoleConfig for scheduled ops.
    """
    return AGENT_OP_SKILLS.copy()
#
