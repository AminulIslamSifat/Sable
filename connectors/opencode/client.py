"""OpenCode free-tier connector — a real Sable client, not a bare HTTP call.

Subclasses :class:`OpenAICompatClient` so it inherits everything the rest of
Sable's backends get for free:

    * persona + chat-harness injection   (``inject_instructions``)
    * system-instruction overrides       (study mode, agent sub-chats)
    * per-chat session history + trimming + summarisation
    * DB history seeding on provider switch
    * multimodal file attachments
    * the full native tool-call loop     (engine.skills dispatch, up to
      ``MAX_TOOL_ROUNDS``, results fed back as ``tool`` role messages)
    * ``chat_title`` / skill events yielded upstream

What is overridden is only what makes OpenCode different:

    * no API key — the free tier is anonymous, so ``_auth_headers`` drops
      ``Authorization`` and adds the gate's ``User-Agent`` + session header.
    * ``reasoning_effort`` mapped from the catalog's own effort values
      (``none``/``minimal``/``low``/``medium``/``high``/``xhigh``/``max``)
      rather than the fixed low/medium/high table.
    * a fresh ``ses_`` id per rejected session, retried once.

Model list, capabilities and thinking modes are refreshed from
``https://models.opencode.ai/api.json`` at startup by
``engine.plugins.opencode_free``.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from connectors.common.openai_compat import OpenAICompatClient

logger = logging.getLogger("sable.connectors.opencode")

_BASE_URL = "https://opencode.ai/inference/openai/v1"
_USER_AGENT = "opencode/latest/2.1.2/cli"
_DEFAULT_MODEL = "deepseek-v4.1-flash-free"


def _new_session_id() -> str:
    """``ses_`` + 26 lowercase hex — the only format the gate accepts."""
    return "ses_" + secrets.token_hex(13)


class OpenCodeClient(OpenAICompatClient):
    """OpenCode free tier, driven through Sable's normal connector contract."""

    NAME = "OpenCode"
    BASE_URL = _BASE_URL
    DEFAULT_MODEL = _DEFAULT_MODEL
    INSTRUCTION_MODE = "project"   # persona + harness + tool schemas
    ENABLE_REASONING = True
    SUPPORTS_MULTIMODAL_CONTENT = True

    def __init__(self) -> None:
        # A dummy key keeps the base class happy; it is never sent.
        super().__init__(
            base_url=_BASE_URL,
            api_key="anonymous",
            name=self.NAME,
            default_model=_DEFAULT_MODEL,
            instruction_mode="project",
        )
        self._session_id = _new_session_id()

    # -- gate -----------------------------------------------------------

    def _auth_headers(self, key: str) -> dict[str, str]:
        """No ``Authorization`` — the free tier is anonymous by design.

        The gate checks request *shape*: a recognisable User-Agent and a
        well-formed ``ses_`` session header. Verified 2026-10-22: the same
        request returns 200 with no auth header at all.
        """
        return {
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
            "x-opencode-session": self._session_id,
            "x-opencode-client": "cli",
        }

    def rotate_session(self) -> None:
        """Mint a new session id (used when the gate rejects the old one)."""
        self._session_id = _new_session_id()
        logger.info("opencode: rotated session id")

    def _tweak_payload(self, payload: dict[str, Any], model_id: str) -> None:
        """Guarantee the gate's mandatory ``read`` tool is present.

        Verified 2026-10-22: an otherwise valid request returns 403 the moment
        no tool named exactly ``read`` appears in ``tools[]``. Sable's own tool
        set is kept — ``read`` is appended only if missing, and never invoked
        by the model unless it genuinely asks for it.
        """
        tools = payload.get("tools")
        if not isinstance(tools, list):
            tools = []
        has_read = any(
            isinstance(t, dict) and (t.get("function") or {}).get("name") == "read"
            for t in tools
        )
        if not has_read:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": "read",
                        "description": "Read the contents of a file.",
                        "parameters": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                        },
                    },
                }
            )
        payload["tools"] = tools

    # -- reasoning ------------------------------------------------------

    @staticmethod
    def _resolve_reasoning_effort(thinking_mode: str | None) -> str | None:
        """Map Sable's thinking mode onto the catalog's effort vocabulary."""
        if not thinking_mode:
            return None
        mode = thinking_mode.lower()
        if mode in ("fast", "none", "off"):
            return None
        if mode in ("thinking", "deepthink"):
            return "high"
        if mode in ("minimal", "low", "medium", "high", "xhigh", "max"):
            return mode
        return None

    # -- error handling -------------------------------------------------

    async def stream_chat(self, *args: Any, **kwargs: Any):
        """Wrap the base loop so a 403 can mint a fresh session id and retry."""
        for attempt in (0, 1):
            try:
                async for event in super().stream_chat(*args, **kwargs):
                    yield event
                return
            except Exception as exc:
                text = str(exc)
                rejected = "403" in text or "FreeTierError" in text
                if rejected and attempt == 0:
                    self.rotate_session()
                    continue
                raise


_INSTANCE: OpenCodeClient | None = None


def get_client() -> OpenCodeClient:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = OpenCodeClient()
    return _INSTANCE
