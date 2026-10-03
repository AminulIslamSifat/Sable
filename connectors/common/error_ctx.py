"""Shared error context for connector error messages.

Every browser-backed connector (Mistral, MiMo, Gemini Web, Kimi, Qwen) yields
``{"type": "error", "message": ...}`` dicts. Without account/model context the
UI shows bare strings like "Message rate limit reached (retry after 1155s)"
with no indication of *which* profile or provider hit the wall.

Usage in a connector client:
    from connectors.common.error_ctx import ErrorContext

    class MyClient:
        def __init__(self) -> None:
            self.ctx = ErrorContext("my_provider")

        def set_account(self, account: str) -> None:
            self.ctx.account = account

        async def stream(self, ...):
            ...
            yield self.ctx.error("rate limited", status_code=429)
"""
from __future__ import annotations

import re
from typing import Any


class ErrorContext:
    """Mutable bag of context that enriches error messages."""

    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.account: str | None = None
        self.model: str | None = None
        self.browser_path: str | None = None
        self._provider_key: str | None = None

    def set_account_resolver(self, fn, provider: str | None = None) -> None:
        """Accept a callable that returns the account a provider is using.

        Resolved lazily at error-time rather than pushed by every caller.
        When ``provider`` is given the callable is invoked as ``fn(provider)``
        so it can honour the provider's own active-account slot instead of
        reporting the global active account.
        """
        self._account_fn = fn
        self._provider_key = provider

    def _resolve_account(self) -> str | None:
        if self.account:
            return self.account
        fn = getattr(self, "_account_fn", None)
        if fn is not None:
            try:
                return fn(self._provider_key) if self._provider_key else fn()
            except Exception:
                pass
        return None

    @property
    def prefix(self) -> str:
        """Build the ``[provider | account]`` tag."""
        parts = [self.provider]
        acct = self._resolve_account()
        if acct:
            # Shorten "browser-data-acc3" → "acc3"
            m = re.match(r"browser-data-(acc\d+)$", acct)
            parts.append(m.group(1) if m else acct)
        if self.model:
            parts.append(self.model)
        return "[" + " | ".join(parts) + "]"

    def error(self, message: str, **extra: Any) -> dict[str, Any]:
        """Return an enriched error event dict."""
        enriched = f"{self.prefix} {message}"
        ev: dict[str, Any] = {"type": "error", "message": enriched}
        if extra:
            ev.update(extra)
        return ev

    def fmt(self, template: str, *args: Any) -> str:
        """Format a string with the context prefix prepended."""
        msg = template.format(*args) if args else template
        return f"{self.prefix} {msg}"
