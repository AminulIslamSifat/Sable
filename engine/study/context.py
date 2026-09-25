"""Per-request study context for skill handlers.

Skill handlers receive only ``(tag_id, name, attrs, content)`` — they have no
argument for "which document is open". A ContextVar carries that ambient state
from the study chat route down to the handlers without threading it through the
engine, and without globals leaking across concurrent requests.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass
class StudyContext:
    """Ambient study-mode state for the duration of one chat turn."""
    doc_id: str = ""          # document open in the reader, if any
    page: int = 0             # reader's current page
    events: list[dict[str, Any]] = field(default_factory=list)  # side-channel events
    changed: bool = False     # did a handler mutate study state?

    def emit(self, event: dict[str, Any]) -> None:
        """Queue an event for the route to forward over SSE."""
        self.events.append(event)

    def mark_changed(self, kind: str, **extra: Any) -> None:
        self.changed = True
        self.emit({"type": "doc_changed", "kind": kind, **extra})


_study_ctx: ContextVar[StudyContext | None] = ContextVar("sable_study_ctx", default=None)


def set_study_context(ctx: StudyContext | None):
    """Install the context for this turn; returns a reset token."""
    return _study_ctx.set(ctx)


def reset_study_context(token) -> None:
    if token is None:
        _study_ctx.set(None)
        return
    try:
        _study_ctx.reset(token)
    except (ValueError, LookupError, TypeError):
        pass


def get_study_context() -> StudyContext | None:
    return _study_ctx.get()
