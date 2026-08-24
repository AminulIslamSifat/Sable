
"""SecurityMiddleware — replaces the stub PermissionMiddleware.

Checks tag content for injection patterns, gates destructive commands,
validates paths, and enforces approval gates for sensitive operations.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Generator

from engine.skills.events import end_event, permission_request_event
from engine.skills.middleware import TagContext
from engine.security.prompt_guard import PromptGuard, Severity

logger = logging.getLogger(__name__)


def _get_ssd_tree() -> str:
    """Lazy-load SSD_TREE from config to avoid circular imports."""
    from engine.config import SSD_TREE
    return SSD_TREE


# ─── Hard-blocked commands (instant reject, no negotiation) ───────────────────
_BLOCKED_COMMANDS = re.compile(
    r"(?:rm\s+-rf\s+/(?:\s|$|\*)|rm\s+-rf\s+~(?:\s|$)|mkfs\.|dd\s+if=/dev/(?:zero|random)\s+of=/dev/|"
    r">\s*/dev/sd|reboot|shutdown\s|halt\s|poweroff)",
    re.I,
)

# ─── Permission-required commands (need explicit user approval) ───────────────
# Each entry: (compiled_regex, category, human_reason)
_PERMISSION_REQUIRED: list[tuple[re.Pattern, str, str]] = [
    # File system destruction (targeted)
    (re.compile(r"rm\s+-rf\s+(?!/|~\s*$)(?:~/|\$HOME)", re.I), "filesystem", "Recursive delete on home directory contents"),
    (re.compile(r"rm\s+-(?:r|f|rf|fr)\s+(?!/tmp)", re.I), "filesystem", "Recursive/force file deletion"),
    (re.compile(r"shred\s|truncate\s+-s\s*0", re.I), "filesystem", "Secure delete / file truncation"),
    (re.compile(r"chmod\s+777|chmod\s+-R", re.I), "filesystem", "Broad permission change"),
    (re.compile(r"chown\s", re.I), "filesystem", "Ownership change"),

    # Package management
    (re.compile(r"pacman\s+-(?:R|S)\b", re.I), "packages", "System package install/remove"),
    (re.compile(r"(?:yay|paru)\s+-(?:R|S)\b", re.I), "packages", "AUR package install/remove"),
    (re.compile(r"pip\s+uninstall|uv\s+remove", re.I), "packages", "Python package removal"),

    # Service management
    (re.compile(r"systemctl\s+(?:stop|disable|restart|mask)\s", re.I), "services", "Systemd service control"),

    # Git destructive
    (re.compile(r"git\s+push\s+(?:--force|-f)\b", re.I), "git", "Force push (overwrites remote history)"),
    (re.compile(r"git\s+branch\s+-D\b", re.I), "git", "Force delete branch"),
    (re.compile(r"git\s+reset\s+--hard\b", re.I), "git", "Hard reset (discards uncommitted changes)"),
    (re.compile(r"git\s+clean\s+-[a-z]*f", re.I), "git", "Remove untracked files"),

    # Communication (irreversible send)
    (re.compile(r"curl\s+.*-X\s*(?:POST|PUT|DELETE|PATCH)", re.I), "network", "HTTP write request to external API"),
    (re.compile(r"wget\s+.*\|\s*(?:ba)?sh|curl\s+.*\|\s*(?:ba)?sh", re.I), "network", "Pipe-to-shell execution"),

    # Auth / security
    (re.compile(r"(?:cat|cp|mv|rm|chmod|echo)\s+.*~?/?\.ssh/", re.I), "auth", "SSH key manipulation"),
    (re.compile(r"gpg\s+--(?:delete|revoke)", re.I), "auth", "GPG key deletion/revocation"),
    (re.compile(r"usermod|useradd|userdel|passwd\s", re.I), "auth", "User account modification"),
    (re.compile(r"(?:ufw|iptables|nft)\s", re.I), "auth", "Firewall rule change"),
    (re.compile(r"/etc/(?:passwd|sudoers|shadow)", re.I), "auth", "System auth file access"),

    # Disk / block devices
    (re.compile(r"\bdd\s+", re.I), "disk", "Raw disk write (dd)"),
    (re.compile(r"fdisk\s|parted\s", re.I), "disk", "Partition table modification"),
    (re.compile(r"mount\s|umount\s", re.I), "disk", "Mount/unmount filesystem"),
    (re.compile(r"swapoff\s", re.I), "disk", "Disable swap"),

    # Process management
    (re.compile(r"kill\s+-9\s|killall\s|pkill\s", re.I), "process", "Force kill processes"),

    # Database
    (re.compile(r"(?:DROP|TRUNCATE)\s+(?:TABLE|DATABASE)", re.I), "database", "Destructive SQL operation"),
    (re.compile(r"DELETE\s+FROM\s+\w+\s*(?:;|$)", re.I), "database", "DELETE without WHERE clause"),

    # System state
    (re.compile(r"crontab\s+-r", re.I), "system", "Wipe entire crontab"),
    (re.compile(r"sysctl\s+-w", re.I), "system", "Runtime kernel parameter change"),

    # SSD tree write guard — pattern built at import time from config
    (re.compile(rf"(?:cp|mv|tee|cat\s*>|echo\s*>)\s+.*?{re.escape(_get_ssd_tree())}", re.I), "filesystem", "Direct write to SSD Sable tree (edit HDD first)"),
]

# Tags whose content is a shell command
_COMMAND_TAGS = frozenset({"execute_command"})


# ─── Pending approvals store ──────────────────────────────────────────────────
@dataclass
class PendingApproval:
    tag_id: str
    name: str
    attrs: dict[str, str]
    content: str
    category: str
    reason: str
    created: float = field(default_factory=time.time)


# In-memory store: tag_id → PendingApproval (expires after 5 min)
_pending_approvals: dict[str, PendingApproval] = {}
_APPROVAL_TTL = 300  # seconds

# ─── Session permission cache ─────────────────────────────────────────────────
# chat_id → set of approved categories (persists for server lifetime)
_session_permissions: dict[str, set[str]] = {}


def cache_session_permission(chat_id: str, category: str) -> None:
    """Cache an approved category for the session (chat)."""
    if chat_id:
        _session_permissions.setdefault(chat_id, set()).add(category)
        logger.info("SESSION PERM CACHED: chat=%s category=%s", chat_id, category)


def is_session_permitted(chat_id: str | None, category: str) -> bool:
    """Check if a category is already approved for this session."""
    if not chat_id:
        return False
    return category in _session_permissions.get(chat_id, set())


def clear_session_permissions(chat_id: str) -> None:
    """Clear all cached permissions for a chat (e.g. on chat delete)."""
    _session_permissions.pop(chat_id, None)


def get_pending_approval(tag_id: str) -> PendingApproval | None:
    """Retrieve a pending approval by tag_id. Returns None if expired."""
    entry = _pending_approvals.get(tag_id)
    if entry is None:
        return None
    if time.time() - entry.created > _APPROVAL_TTL:
        _pending_approvals.pop(tag_id, None)
        return None
    return entry


def consume_pending_approval(tag_id: str) -> PendingApproval | None:
    """Retrieve and remove a pending approval (for execution after user approves)."""
    entry = get_pending_approval(tag_id)
    if entry:
        _pending_approvals.pop(tag_id, None)
    return entry


def deny_pending_approval(tag_id: str) -> bool:
    """Remove a pending approval (user denied). Returns True if it existed."""
    return _pending_approvals.pop(tag_id, None) is not None


def check_permission_required(content: str) -> tuple[str, str] | None:
    """Check if a command matches any permission-required pattern.

    Returns (category, reason) if approval needed, None if clear.
    """
    for pattern, category, reason in _PERMISSION_REQUIRED:
        if pattern.search(content):
            return category, reason
    return None


class SecurityMiddleware:
    """Drop-in replacement for PermissionMiddleware with real checks.

    Pipeline position: after Validation, before Execution.
    Can short-circuit by setting ctx.error.
    """

    def __init__(
        self,
        extra_blocked_patterns: list[tuple[str, str, Severity]] | None = None,
    ) -> None:
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

        # --- 2. Gate destructive shell commands (hard block) ---
        if ctx.name in _COMMAND_TAGS and ctx.content:
            if _BLOCKED_COMMANDS.search(ctx.content):
                ctx.error = "Destructive command blocked by security policy"
                logger.warning("SECURITY BLOCK: destructive command in %s", ctx.name)
                yield end_event(ctx.tag_id, ctx.name, False, ctx.started, error=ctx.error)
                return

        # --- 3. Permission-required gate (approval flow) ---
        if ctx.name in _COMMAND_TAGS and ctx.content:
            # Skip if already approved (re-submission after user approval)
            if ctx.attrs.get("approved") == "true":
                logger.info("APPROVED: tag=%s command=%s", ctx.name, ctx.content[:80])
            else:
                match = check_permission_required(ctx.content)
                if match:
                    category, reason = match
                    # Check session cache — skip prompt if already approved for this chat
                    if is_session_permitted(ctx.chat_id, category):
                        logger.info(
                            "SESSION PERM HIT: tag=%s category=%s chat=%s",
                            ctx.name, category, ctx.chat_id,
                        )
                    else:
                        # Store pending approval
                        _pending_approvals[ctx.tag_id] = PendingApproval(
                            tag_id=ctx.tag_id,
                            name=ctx.name,
                            attrs=ctx.attrs,
                            content=ctx.content,
                            category=category,
                            reason=reason,
                        )
                        logger.info(
                            "PERMISSION REQUIRED: tag=%s category=%s cmd=%s",
                            ctx.name, category, ctx.content[:100],
                        )
                        # Yield the permission request event to frontend
                        yield permission_request_event(
                            ctx.tag_id, ctx.name, ctx.content, category, reason,
                        )
                        # Stop pipeline — do NOT execute
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


#
