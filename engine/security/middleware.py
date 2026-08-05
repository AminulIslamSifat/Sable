
"""SecurityMiddleware — replaces the stub PermissionMiddleware.

Checks tag content for injection patterns, gates destructive commands,
and validates paths before execution reaches the handler.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Generator

from engine.skills.events import end_event
from engine.skills.middleware import TagContext
from engine.security.prompt_guard import PromptGuard, Severity

logger = logging.getLogger(__name__)

# Commands that require explicit confirmation or are always blocked
_BLOCKED_COMMANDS = re.compile(
    r"(?:rm\s+-rf\s+/|rm\s+-rf\s+~|mkfs\.|dd\s+if=/dev/(?:zero|random)\s+of=/dev/|"
    r">\s*/dev/sd|shutdown|reboot|halt|poweroff|init\s+0|init\s+6|"
    r"chmod\s+777\s+/|chown\s+-R\s+.*\s+/)",
    re.I,
)

# Tags whose content is a shell command
_COMMAND_TAGS = frozenset({"execute_command", "execute_background_command"})

# Tags that write to the filesystem
_WRITE_TAGS = frozenset({"create_file", "edit_file", "insert_file", "create_note", "save_svg"})

# Allowed write roots (configurable at init)
_DEFAULT_WRITE_ROOTS = (
    "/home/sifat/Projects/Sable/",
    "/home/sifat/hdd/",
    "/tmp/",
)


class SecurityMiddleware:
    """Drop-in replacement for PermissionMiddleware with real checks.

    Pipeline position: after Validation, before Execution.
    Can short-circuit by setting ctx.error.
    """

    def __init__(
        self,
        write_roots: tuple[str, ...] = _DEFAULT_WRITE_ROOTS,
        extra_blocked_patterns: list[tuple[str, str, Severity]] | None = None,
    ) -> None:
        self._write_roots = write_roots
        self._guard = PromptGuard(extra_patterns=extra_blocked_patterns)

    def process(
        self,
        ctx: TagContext,
        next_fn: Callable[[TagContext], Generator[dict[str, Any], None, None]],
    ) -> Generator[dict[str, Any], None, None]:
        # --- 1. Scan tag content for injection patterns ---
        if ctx.content:
            verdict = self._guard.scan(ctx.content)
            if verdict.blocked:
                logger.warning(
                    "SECURITY BLOCK: tag=%s rules=%s detail=%s",
                    ctx.name, verdict.matched_rules, verdict.detail,
                )
                ctx.error = f"Blocked by security policy: {verdict.detail}"
                yield end_event(ctx.tag_id, ctx.name, False, ctx.started, error=ctx.error)
                return
            if verdict.warned:
                logger.info(
                    "SECURITY WARN: tag=%s rules=%s",
                    ctx.name, verdict.matched_rules,
                )

        # --- 2. Gate destructive shell commands ---
        if ctx.name in _COMMAND_TAGS and ctx.content:
            if _BLOCKED_COMMANDS.search(ctx.content):
                ctx.error = f"Destructive command blocked by security policy"
                logger.warning("SECURITY BLOCK: destructive command in %s", ctx.name)
                yield end_event(ctx.tag_id, ctx.name, False, ctx.started, error=ctx.error)
                return

        # --- 3. Validate write paths stay within allowed roots ---
        if ctx.name in _WRITE_TAGS:
            path = ctx.attrs.get("path", "")
            if path and not self._is_path_allowed(path):
                ctx.error = f"Write to '{path}' denied — outside allowed roots"
                logger.warning("SECURITY BLOCK: path %s outside write roots", path)
                yield end_event(ctx.tag_id, ctx.name, False, ctx.started, error=ctx.error)
                return

        # --- 4. Scan attrs for injection (e.g. URLs in browser tags) ---
        for key, value in ctx.attrs.items():
            if value and len(value) > 20:
                verdict = self._guard.scan(value)
                if verdict.blocked:
                    ctx.error = f"Blocked attr '{key}' by security policy: {verdict.detail}"
                    yield end_event(ctx.tag_id, ctx.name, False, ctx.started, error=ctx.error)
                    return

        # All checks passed — continue pipeline
        yield from next_fn(ctx)

    def _is_path_allowed(self, path: str) -> bool:
        """Check if a filesystem path falls within allowed write roots."""
        # Normalize: resolve ~ and relative components
        import os
        resolved = os.path.normpath(os.path.expanduser(path))

        for root in self._write_roots:
            norm_root = os.path.normpath(root)
            if resolved.startswith(norm_root):
                return True

        # Allow /tmp unconditionally (scratch space)
        if resolved.startswith("/tmp/"):
            return True

        return False

