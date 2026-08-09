
"""Security layer for Sable — prompt injection guard, content sanitization, tool policy."""

from engine.security.prompt_guard import (
    InjectionVerdict,
    PromptGuard,
    Severity,
    scan_content,
    wrap_untrusted,
)
from engine.security.middleware import (
    SecurityMiddleware,
    PendingApproval,
    check_permission_required,
    consume_pending_approval,
    deny_pending_approval,
    get_pending_approval,
)

__all__ = [
    "InjectionVerdict",
    "PendingApproval",
    "PromptGuard",
    "SecurityMiddleware",
    "Severity",
    "check_permission_required",
    "consume_pending_approval",
    "deny_pending_approval",
    "get_pending_approval",
    "scan_content",
    "wrap_untrusted",
]

