
"""Base protocol for Sable API connectors.

Every connector must implement this interface so the chat route can
use them interchangeably. Duck-typing is fine — no need to inherit,
but this Protocol documents the contract and enables type-checking.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ConnectorProtocol(Protocol):
    """Minimal interface every API connector must satisfy."""

    async def stream_chat(
        self,
        message: str,
        *,
        model: str | None = None,
        thinking_mode: str | None = None,
        chat_id: str | None = None,
        ref_file_ids: list[str] | None = None,
        inject_instructions: bool = True,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Stream response events.

        Yields dicts with at minimum a "type" key:
          {"type": "answer",   "text": "..."}
          {"type": "thinking", "text": "..."}
          {"type": "done",     "parent_id": "..."}
          {"type": "error",    "message": "..."}
        """
        ...  # pragma: no cover

    async def chat(
        self,
        message: str,
        *,
        model: str | None = None,
        thinking_mode: str | None = None,
        chat_id: str | None = None,
        ref_file_ids: list[str] | None = None,
        inject_instructions: bool = True,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Non-streaming chat. Returns {answer, thinking, parent_id, error}."""
        ...  # pragma: no cover

    @property
    def is_available(self) -> bool:
        """Whether the connector is ready (e.g. has valid credentials)."""
        ...  # pragma: no cover
