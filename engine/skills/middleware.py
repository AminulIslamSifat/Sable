
"""Middleware pipeline for skill tag execution.

Pipeline order: Validation → Permission → Execution → Logging
Each middleware can short-circuit by setting ctx.error and returning.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Generator, Protocol

from engine.skills.events import end_event, output_event, start_event
from engine.skills.registry import SkillMeta

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TagContext:
    """Carries state through the middleware pipeline for one tag execution."""

    tag_id: str
    name: str
    attrs: dict[str, str]
    content: str
    skill: SkillMeta | None = None
    started: float = field(default_factory=time.time)
    events: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    namespace: str = "default"
    chat_id: str | None = None

    def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)


class Middleware(Protocol):
    """Protocol for pipeline middleware."""

    def process(self, ctx: TagContext, next_fn: Callable[[TagContext], Generator[dict[str, Any], None, None]]) -> Generator[dict[str, Any], None, None]:
        ...


class ValidationMiddleware:
    """Validates tag is known, skill exists, and content is non-empty where required."""

    def __init__(self, tag_ownership: dict[str, SkillMeta], handler_keys: set[str] | None = None) -> None:
        self._ownership = tag_ownership
        self._handler_keys = handler_keys or set()

    def process(self, ctx: TagContext, next_fn: Callable[[TagContext], Generator[dict[str, Any], None, None]]) -> Generator[dict[str, Any], None, None]:
        # Resolve owning skill
        skill = self._ownership.get(ctx.name) or self._ownership.get(ctx.name.replace("-", "_"))
        if skill is None:
            # Allow tool-only tags (e.g. code_editor tools) that have handlers
            # but no skill ownership entry
            normalized = ctx.name.replace("-", "_")
            if ctx.name in self._handler_keys or normalized in self._handler_keys:
                ctx.skill = None  # No owning skill, but handler exists
            else:
                ctx.error = f"No handler for tag '{ctx.name}'"
                yield end_event(ctx.tag_id, ctx.name, False, ctx.started, error=ctx.error)
                return
        else:
            ctx.skill = skill

        # Content-required tags
        content_required = {"execute_command", "create_file", "create_note", "save_svg", "create_svg"}
        if ctx.name in content_required and not ctx.content.strip():
            ctx.error = f"Tag '{ctx.name}' requires non-empty content"
            yield end_event(ctx.tag_id, ctx.name, False, ctx.started, error=ctx.error)
            return

        yield from next_fn(ctx)


class PermissionMiddleware:
    """Permission checks (stub — pass-through for now).

    Future: scope validation, dangerous command confirmation,
    per-agent permission policies.
    """

    def process(self, ctx: TagContext, next_fn: Callable[[TagContext], Generator[dict[str, Any], None, None]]) -> Generator[dict[str, Any], None, None]:
        # Pass-through — all tags permitted
        yield from next_fn(ctx)


class ExecutionMiddleware:
    """Dispatches to the registered handler function."""

    def __init__(self, handlers: dict[str, Callable[..., Generator[dict[str, Any], None, None]]]) -> None:
        self._handlers = handlers

    def process(self, ctx: TagContext, next_fn: Callable[[TagContext], Generator[dict[str, Any], None, None]]) -> Generator[dict[str, Any], None, None]:
        handler = self._handlers.get(ctx.name) or self._handlers.get(ctx.name.replace("-", "_"))
        if handler is None:
            ctx.error = f"No handler registered for '{ctx.name}'"
            yield end_event(ctx.tag_id, ctx.name, False, ctx.started, error=ctx.error)
            return

        # Emit start event
        start = start_event(ctx.tag_id, ctx.name, ctx.attrs, ctx.content)
        ctx.emit(start)
        yield start

        # Run handler
        saw_end = False
        try:
            for event in handler(ctx.tag_id, ctx.name, ctx.attrs, ctx.content):
                if event.get("type") == "skill_end":
                    saw_end = True
                ctx.emit(event)
                yield event

            if not saw_end:
                ev = end_event(ctx.tag_id, ctx.name, True, ctx.started)
                ctx.emit(ev)
                yield ev
        except Exception as exc:
            ctx.error = f"{type(exc).__name__}: {exc}"
            ev = end_event(ctx.tag_id, ctx.name, False, ctx.started, error=ctx.error)
            ctx.emit(ev)
            yield ev

        yield from next_fn(ctx)


class LoggingMiddleware:
    """Logs tag execution (stub — logs at debug level)."""

    def process(self, ctx: TagContext, next_fn: Callable[[TagContext], Generator[dict[str, Any], None, None]]) -> Generator[dict[str, Any], None, None]:
        logger.debug("Tag exec: %s (skill=%s)", ctx.name, ctx.skill.key if ctx.skill else "?")
        yield from next_fn(ctx)
        duration = int((time.time() - ctx.started) * 1000)
        logger.debug("Tag done: %s ok=%s %dms", ctx.name, ctx.error is None, duration)


class MiddlewarePipeline:
    """Ordered middleware chain. Each middleware wraps the next."""

    def __init__(self, middlewares: list[Any] | None = None) -> None:
        self._middlewares: list[Any] = middlewares or []

    def add(self, middleware: Any) -> None:
        self._middlewares.append(middleware)

    def execute(self, ctx: TagContext) -> Generator[dict[str, Any], None, None]:
        """Run the full pipeline for a tag context."""

        def terminal(c: TagContext) -> Generator[dict[str, Any], None, None]:
            # End of chain — no-op
            return
            yield  # make it a generator

        # Build the chain from inside out
        chain = terminal
        for mw in reversed(self._middlewares):
            chain = _wrap(mw, chain)

        yield from chain(ctx)


def _wrap(middleware: Any, next_fn: Callable[[TagContext], Generator[dict[str, Any], None, None]]) -> Callable[[TagContext], Generator[dict[str, Any], None, None]]:
    """Wrap a middleware around a next function."""
    def wrapped(ctx: TagContext) -> Generator[dict[str, Any], None, None]:
        yield from middleware.process(ctx, next_fn)
    return wrapped
