
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
    cwd: str | None = None

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

        # NOTE: the old `content_required` set was removed. It was redundant with
        # SchemaValidationMiddleware (which checks required args against the real
        # tool.json) and subtly wrong — `execute_command` supplies its command via
        # attrs, not the tag body. Schema validation is now the single source of
        # truth for what a tool needs.

        yield from next_fn(ctx)


# NOTE: PermissionMiddleware used to live here as a pass-through stub. It was
# dead code — engine/skills/engine.py wires in SecurityMiddleware
# (engine/security/middleware.py) instead, which does the real work: prompt
# injection guard, destructive-command gate, CWD guard, approval flow.
# Removed to avoid implying permissions are unenforced.


class SchemaValidationMiddleware:
    """Validates tag attrs against the tool's JSON schema before execution.

    Turns permissive-schema runtime errors ("missing value", "unknown action")
    into clean pre-execution failures with actionable messages. Uses the
    lightweight validator in engine.skills.schema_check — no jsonschema dep.

    Runs after SecurityMiddleware so security blocks still take precedence.
    """

    def process(self, ctx: TagContext, next_fn: Callable[[TagContext], Generator[dict[str, Any], None, None]]) -> Generator[dict[str, Any], None, None]:
        try:
            from engine.skills.schema_check import validate_arguments
            from engine.tools_loader import load_single_tool
        except Exception:
            # If the loader isn't importable, don't block execution
            yield from next_fn(ctx)
            return

        schema_list = load_single_tool(ctx.name)
        if not schema_list:
            yield from next_fn(ctx)
            return

        params = (schema_list[0].get("function") or {}).get("parameters") or {}
        ok, errors = validate_arguments(ctx.attrs, params)
        if not ok:
            detail = "; ".join(errors)
            ctx.error = f"Invalid arguments for '{ctx.name}': {detail}"
            logger.warning("SCHEMA REJECT: tag=%s %s", ctx.name, detail)
            yield end_event(ctx.tag_id, ctx.name, False, ctx.started, error=ctx.error)
            return

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

        # Inject chat_id into attrs so handlers can access current chat context
        if ctx.chat_id and "_chat_id" not in ctx.attrs:
            ctx.attrs["_chat_id"] = ctx.chat_id

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
