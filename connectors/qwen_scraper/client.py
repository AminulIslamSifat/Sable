"""Qwen scraper connector — headed-browser DOM/SSE transport.

This is the *experimental*, per-model alternative to the pure-API Qwen
transport (engine/chat.py curl + engine/inpage.py ghost). It drives the real
chat.qwen.ai SPA in a headed browser using the same account profile the Qwen
ghost uses, and scrapes the DOM + taps the in-page SSE stream instead of
calling the API from Python. Same login, different transport.

Selectable as its own model (``api_backend = "qwen_scraper"``) — it is NOT
tied to the global scraper-mode toggle.

Qwen's completions stream (verified against engine/chat.py) is an
OpenAI-shaped SSE:
    data: {"choices":[{"delta":{"phase":"think","content":"..."}}],"response_id":"..."}
    data: {"choices":[{"delta":{"phase":"answer","content":"..."}}]}
    data: {"response.created":{"response_id":"...","response_index":"0"}}
Phase routing:
  * ``think`` / ``thinking`` / ``thinking_summary`` -> reasoning stream
  * ``answer``                                      -> answer stream
"""
from __future__ import annotations

import json
from typing import Any

from connectors.common.browser_scraper import BaseBrowserEngine, ScraperSpec
from connectors.common.scraper_connector import BaseScraperConnector

SELECTORS: dict[str, str] = {
    # Composer. Qwen ships a contenteditable / textarea pair depending on
    # build; both are covered (input helper falls back to generic selectors).
    "input": "textarea.message-input-textarea, textarea[placeholder='How can I help you today?']",
    "send_btn": "div.message-input-right-button-send button.send-button",
    "response": "div.response-message-content",
    "content": "div.response-message-content",
    "stop": "div.chat-prompt-send-button button.stop-button",
    # Attach (+) button — opens the native file chooser for image/file upload.
    "attach_btn": "div.message-input-left-button button, button[aria-label*='ttach']",
}

SPEC = ScraperSpec(
    provider="qwen",
    url="https://chat.qwen.ai/",
    selectors=SELECTORS,
    keys={"send": "Enter"},
    sse_target="/api/v2/chat/completions",
    stealth_mode=True,
    instruction_provider="qwen",
)

# Qwen chat URLs look like https://chat.qwen.ai/c/<chat_id>
import re as _re
_SESSION_ID_RE = _re.compile(r"/c/([^/?#]+)")


class QwenScraperEngine(BaseBrowserEngine):
    """Qwen SPA driver: OpenAI-shaped SSE delta parsing."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Qwen patch state (reset per request).
        self._sse_line_buf: str = ""
        self._sse_seen_content: bool = False
        self._qwen_response_id: str | None = None
        # Cumulative length of the thinking_summary already emitted — Qwen
        # re-sends the whole growing list each event, so we emit only the tail.
        self._summary_sent: str = ""

    def _reset_sse_capture(self) -> None:
        super()._reset_sse_capture()
        self._sse_line_buf = ""
        self._sse_seen_content = False
        self._qwen_response_id = None
        self._summary_sent = ""

    def _parse_qwen_obj(self, obj: dict[str, Any]) -> tuple[str, str]:
        """Map one Qwen SSE object to (content_delta, reasoning_delta).

        Mirrors engine/chat.py's curl parser so both transports decode the
        same wire format identically.
        """
        # response.created carries the id we track as the upstream parent.
        created = obj.get("response.created")
        if isinstance(created, dict):
            rid = created.get("response_id")
            if isinstance(rid, str) and (
                created.get("response_index") == "0" or self._qwen_response_id is None
            ):
                self._qwen_response_id = rid

        rid = obj.get("response_id")
        if isinstance(rid, str):
            if self._qwen_response_id is None:
                self._qwen_response_id = rid
            elif rid != self._qwen_response_id:
                # A stale response id from a prior turn — ignore its deltas.
                return "", ""

        choices = obj.get("choices", [])
        if not choices or not isinstance(choices, list):
            return "", ""
        delta = choices[0].get("delta", {}) if isinstance(choices[0], dict) else {}
        phase = (delta.get("phase", "") or "").lower()
        content = delta.get("content", "") or ""
        status = (delta.get("status", "") or "").lower()

        # Terminal marker: Qwen sends {"phase":"answer","status":"finished"}
        # as the last delta. Flag it so the capture loop can end on the network
        # signal instead of waiting for the stop button to disappear.
        if status in ("finished", "complete", "completed", "done", "stop"):
            self._stream_finished = True

        # Reasoning phases. Qwen sends TWO different shapes here:
        #   * thinking_summary -> extra.summary_thought.content is a LIST that
        #     GROWS each event (cumulative). Naively joining it and appending
        #     per chunk would DUPLICATE the whole thought every tick.
        #   * thinking/think    -> plain incremental content string.
        # We track how much of the cumulative summary we have already emitted
        # and send only the new tail.
        if phase in ("think", "thinking_summary", "thinking"):
            extra = delta.get("extra", {}) or {}
            thought_obj = extra.get("summary_thought", {}) or {}
            thoughts = thought_obj.get("content", [])
            if isinstance(thoughts, list):
                full = "".join(str(t) for t in thoughts)
                prev = self._summary_sent
                if full.startswith(prev):
                    new = full[len(prev):]
                else:
                    # Non-monotonic (panel rebuilt) — emit the tail only if it
                    # is not already a suffix we have sent.
                    new = full if not prev else ""
                self._summary_sent = full if (full.startswith(prev) or not prev) else prev
                return "", new
            text = content
            return "", text

        # Answer phase.
        if phase == "answer" and content:
            return content, ""

        # Some builds omit phase on plain content deltas — treat as answer.
        if content and not phase:
            return content, ""
        return "", ""

    def parse_sse_delta(self, raw_text: str) -> tuple[str, str]:
        """Parse Qwen's SSE stream into (content, reasoning).

        Lines can split across network reads, so a partial trailing line is
        buffered and prepended to the next chunk.
        """
        content_parts: list[str] = []
        reasoning_parts: list[str] = []

        buf = (self._sse_line_buf or "") + (raw_text or "")
        lines = buf.split("\n")
        self._sse_line_buf = lines.pop() if lines else ""

        for line in lines:
            line = line.strip()
            if not line:
                continue
            if line.startswith("data:"):
                payload = line[5:].strip()
            elif line.startswith("{"):
                payload = line
            else:
                continue
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            c, r = self._parse_qwen_obj(obj)
            if c and not self._sse_seen_content:
                self._sse_seen_content = True
                self._log("sse_content_first", chars=len(c))
            if c:
                content_parts.append(c)
            if r:
                reasoning_parts.append(r)

        return "".join(content_parts), "".join(reasoning_parts)

    # -- session URL -----------------------------------------------------

    def session_id_from_url(self, url: str) -> str | None:
        """Extract the Qwen chat id from a chat.qwen.ai URL."""
        m = _SESSION_ID_RE.search(url or "")
        return m.group(1) if m else None


class QwenScraperClient(BaseScraperConnector):
    """Connector wrapping the Qwen headed-browser engine."""

    backend_name = "qwen_scraper"
    provider_name = "qwen"

    def _build_engine(
        self, account: str, browser_data_dir: str | None = None,
        *, adopt_from: "QwenScraperEngine | None" = None,
    ) -> QwenScraperEngine:
        # Honor the account this connector was resolved for (NOT the globally
        # active one). The profile dir and the browser binary both key off the
        # account name, so a pinned-account client opens the right profile +
        # the right browser (Helium/Chrome/Edge per accounts.json).
        from pathlib import Path
        from engine.config import _SYSTEM
        user_data_dir = Path(browser_data_dir) if browser_data_dir else _SYSTEM / account
        if adopt_from is not None:
            # Share the parent's live browser on a new tab. No second launch,
            # no kill_existing (which would murder the parent browser).
            return QwenScraperEngine(
                SPEC,
                viewer=True,
                show_thoughts=True,
                user_data_dir=user_data_dir,
                profile_name=account,
                kill_existing=False,
                adopt_from=adopt_from,
            )
        # Headless is user-controlled from Settings → Account → Qwen
        # (scraper). Default is headed (False) so a first run is visible.
        from engine.scraper import get_settings as _get_scraper_settings
        try:
            _headless = bool(_get_scraper_settings().get("headless", False))
        except Exception:
            _headless = False
        _show_thoughts = True
        try:
            _show_thoughts = bool(_get_scraper_settings().get("show_thoughts", True))
        except Exception:
            pass
        return QwenScraperEngine(
            SPEC,
            viewer=not _headless,      # False -> --headless=new in the launcher
            show_thoughts=_show_thoughts,
            user_data_dir=user_data_dir,
            profile_name=account,
            kill_existing=True,        # kill prior browsers on this profile before opening
        )


_default_client: QwenScraperClient | None = None
_clients: dict[str, QwenScraperClient] = {}


def get_client(account: str | None = None) -> QwenScraperClient:
    """Factory mirroring the other connectors' get_client()."""
    global _default_client
    if account is None:
        if _default_client is None:
            _default_client = QwenScraperClient()
        return _default_client
    if account not in _clients:
        _clients[account] = QwenScraperClient(account=account)
    return _clients[account]


async def cancel_active_streams() -> bool:
    cancelled = False
    for client in list(_clients.values()) + ([_default_client] if _default_client else []):
        try:
            if await client.cancel():
                cancelled = True
        except Exception:
            pass
    return cancelled
