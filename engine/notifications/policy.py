"""Notification policy — decides which desktop notifications fire.

One place owns the "is this worth interrupting the user?" decision so call
sites stay dumb: they report an event, this module decides.

Categories (each independently toggleable in Settings):
  permission  — a tool is blocked waiting for the user's approval
  image       — image generation finished
  research    — deep research finished
  agent       — a spawned subagent finished
  turn        — a long turn (3+ tool calls) finished

Master switch : system setting ``desktop_notifications`` (pre-existing).
Per-category  : system setting ``notification_categories`` = {category: bool}.
"""
from __future__ import annotations

import logging

from engine.notifications.desktop import notify_desktop

logger = logging.getLogger("sable")

# category -> (label, emoji). Order here = display order in Settings.
CATEGORY_META: dict[str, tuple[str, str]] = {
    "permission": ("Approval needed", "🔐"),
    "image":      ("Image ready", "🎨"),
    "research":   ("Research complete", "🔬"),
    "agent":      ("Subagent finished", "🤖"),
    "turn":       ("Turn complete", "✅"),
}

DEFAULT_CATEGORIES: dict[str, bool] = {k: True for k in CATEGORY_META}

# Skill name -> category. Unlisted skills fall through to the generic turn rule.
# NOTE: research is NOT a skill handler — it runs as its own route
# (server/api/routes/research.py) and notifies directly, so it is absent here.
SKILL_CATEGORY: dict[str, str] = {
    "generate_image": "image",
}

# Skills that are bookkeeping, never worth a notification.
_IGNORED_SKILLS = frozenset({"chat_title", "_loop_warning", "action_parse"})


def _settings() -> dict:
    """Read system settings defensively — never raise into a notification path."""
    try:
        from server.api.routes.settings import _read_system_settings
        return _read_system_settings() or {}
    except Exception:
        return {}


def category_enabled(category: str) -> bool:
    """True if this category should fire. The master switch always wins."""
    s = _settings()
    if not s.get("desktop_notifications", True):
        return False
    cats = s.get("notification_categories") or {}
    return bool(cats.get(category, DEFAULT_CATEGORIES.get(category, True)))


async def notify_category(
    category: str,
    title: str,
    body: str,
    *,
    urgency: str = "normal",
) -> None:
    """Fire a desktop notification for ``category`` if that category is enabled."""
    if category not in CATEGORY_META:
        category = "turn"
    if not category_enabled(category):
        return
    _, emoji = CATEGORY_META[category]
    try:
        await notify_desktop(f"{emoji} Sable · {title}", body, urgency=urgency)
    except Exception as exc:  # never let a notification break a turn
        logger.warning("[notify] %s failed: %s", category, exc)


def completed_skills(skill_events: list[dict]) -> list[dict]:
    """Successful, non-bookkeeping skill_end events from a turn."""
    return [
        ev for ev in skill_events
        if ev.get("type") == "skill_end"
        and ev.get("name") not in _IGNORED_SKILLS
    ]


def plan_turn_notifications(skill_events: list[dict]) -> list[tuple[str, str, str, str]]:
    """Decide what to notify at end of turn.

    Returns a list of ``(category, title, body, urgency)`` tuples. Specific
    completions (image, research) win; the generic "turn complete" only fires
    when nothing specific did, so a long turn can't double-notify.
    """
    done = completed_skills(skill_events)
    if not done:
        return []

    # Group successful completions by category.
    by_cat: dict[str, list[dict]] = {}
    for ev in done:
        if not ev.get("ok"):
            continue
        cat = SKILL_CATEGORY.get(ev.get("name", ""))
        if cat:
            by_cat.setdefault(cat, []).append(ev)

    out: list[tuple[str, str, str, str]] = []
    for cat, evs in by_cat.items():
        n = len(evs)
        label = CATEGORY_META[cat][0]
        if n == 1:
            body = f"{evs[0].get('name', 'task')} finished"
        else:
            body = f"{n} {cat} tasks finished"
        out.append((cat, label, body, "normal"))

    if out:
        return out

    # Nothing specific — fall back to the generic long-turn rule.
    if len(done) >= 3:
        ok = sum(1 for ev in done if ev.get("ok"))
        return [(
            "turn",
            "Turn complete",
            f"{len(done)} tools used ({ok} succeeded)",
            "normal",
        )]
    return []