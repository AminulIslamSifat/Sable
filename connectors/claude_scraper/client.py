"""Claude (claude.ai) scraper connector — headed-browser DOM/SSE transport.

Drives the real claude.ai SPA in a headed browser using a logged-in account
profile, taps the in-page SSE stream of the completion endpoint, and falls
back to DOM scrape when the stream misses.

Unlike ChatGPT, Claude does NOT need a ghost-tab fetch hijack: the completion
endpoint is a plain POST returning text/event-stream with no per-message
sentinel / proof-of-work gate. So this rides the generic BaseBrowserEngine the
same way the Qwen and DeepSeek scrapers do.

MODEL SELECTION: deliberately NONE. The engine only types into the composer and
sends — it never touches the model picker. Whatever model the account is set to
on claude.ai itself is what answers. No live model enumeration, no switching.

Verified against a live logged-in session (2026-10-14):

  POST /api/organizations/{org}/chat_conversations/{conv}/completion
    content-type: text/event-stream

  body: {"prompt": "...", "timezone": "Asia/Dhaka", "locale": "en-US",
         "model": "claude-sonnet-5-5", "effort": "medium",
         "thinking_mode": "auto", "tools": [...]}

  SSE wire format (Anthropic Messages style — NOT OpenAI choices[]):
    event: ping                data: {"type":"ping"}
    event: conversation_ready  data: {"type":"conversation_ready"}
    event: message_start       data: {"type":"message_start","message":{...}}
    event: content_block_start data: {"content_block":{"type":"text","text":""}}
    event: content_block_delta data: {"delta":{"type":"text_delta","text":"..."}}
    event: content_block_stop
    event: message_delta       data: {"delta":{"stop_reason":"end_turn"}}
    event: message_stop
"""
from __future__ import annotations

import json
import re
from typing import Any

from connectors.common.browser_scraper import BaseBrowserEngine, ScraperSpec
from connectors.common.scraper_connector import BaseScraperConnector


def claude_headless() -> bool:
    """Read `claude_headless` from system/settings.json.

    Default False = headed. Mirrors `qwen_headless()` in engine/inpage.py and
    `deepseek_headless()` in connectors/deepseek/ghost.py, so every browser
    connector persists its launch mode under its own key instead of sharing
    the global scraper-engine `headless` flag.
    """
    try:
        import json
        from pathlib import Path
        base = Path(__file__).resolve().parent.parent.parent
        path = base / "system" / "settings.json"
        if path.is_file():
            with open(path, "r", encoding="utf-8") as f:
                val = json.load(f).get("claude_headless")
            if val is not None:
                return bool(val)
    except Exception:
        pass
    return False

# All selectors verified live against claude.ai on 2026-10-14.
# NOTE: elements carry NO stable id (React ids like base-ui-_r_5d_ change every
# render) — match by data-testid / role / aria-label only.
SELECTORS: dict[str, str] = {
    "input": "div.ProseMirror",
    "send_btn": "[data-testid='chat-input-send']",
    # Container for one assistant turn. innerText includes a screen-reader
    # prefix ("Claude responded: ...") so prefer the nested "content" selector.
    "response": "[data-testid='assistant-message']",
    "content": "[data-testid='assistant-message'] .font-claude-response",
    "stop": "button[aria-label='Stop response']",
    "attach_btn": "[data-testid='chat-input-attach']",
    # Extended-thinking panel not yet mapped (needs a thinking-enabled capture).
    "thoughts": "",
}

SPEC = ScraperSpec(
    provider="claude",
    url="https://claude.ai/new",
    selectors=SELECTORS,
    keys={"send": "Enter"},
    # Substring match against the fetch URL. The completion endpoint ends in
    # /completion; composer_notices and /title do not contain it.
    sse_target="/completion",
    stealth_mode=True,          # Cloudflare fronts claude.ai
    instruction_provider="claude",
    # Claude's assistant-turn container. Marking THIS is what scopes capture to
    # the current turn; without it `:not([data-ghost-old='true'])` matches every
    # earlier answer on the page.
    old_mark_selector="[data-testid='assistant-message']",
)

_CHAT_ID_RE = re.compile(r"/chat/([^/?#]+)")


class ClaudeScraperEngine(BaseBrowserEngine):
    """claude.ai SPA driver: Anthropic-style SSE event parsing."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # MUST be `_sse_line_buf` — the base engine's get_response() flushes a
        # buffered partial final line via `self._sse_line_buf` at the end of the
        # capture. This engine originally named it `_line_buf`, so that flush
        # raised AttributeError on EVERY turn, the connector classified it as
        # "unknown", tore the browser down, and retried 3× — the browser closed
        # after a perfectly good answer and the stop button was never cleared.
        self._sse_line_buf: str = ""
        self._claude_msg_id: str | None = None

    def _reset_sse_capture(self) -> None:
        super()._reset_sse_capture()
        self._sse_line_buf = ""
        self._claude_msg_id = None

    def _parse_claude_event(self, obj: dict[str, Any]) -> tuple[str, str]:
        """Map one parsed Claude SSE object to (content_delta, reasoning_delta)."""
        etype = obj.get("type", "")

        # message_start carries the assistant message id we report as parent.
        if etype == "message_start":
            msg = obj.get("message") or {}
            mid = msg.get("id") if isinstance(msg, dict) else None
            if isinstance(mid, str):
                self._claude_msg_id = mid
            return "", ""

        # Terminal marker — lets the capture loop end on the network signal
        # instead of waiting for the stop button to disappear.
        if etype == "message_stop":
            self._stream_finished = True
            return "", ""

        if etype in ("content_block_delta", "content_block_start"):
            delta = obj.get("delta")
            block = obj.get("content_block")
            src = delta if isinstance(delta, dict) else (block if isinstance(block, dict) else {})
            dtype = src.get("type", "")
            if dtype == "text_delta":
                return src.get("text", "") or "", ""
            if dtype == "thinking_delta":
                return "", src.get("thinking", "") or ""
            return "", ""

        return "", ""

    def parse_sse_delta(self, raw_text: str) -> tuple[str, str]:
        """Parse one Claude SSE chunk into (content, reasoning).

        Chunks can split mid-line across network reads, so a partial trailing
        line is buffered and prepended to the next read.
        """
        content_parts: list[str] = []
        reasoning_parts: list[str] = []

        buf = (self._sse_line_buf or "") + (raw_text or "")
        lines = buf.split("\n")
        self._sse_line_buf = lines.pop() if lines else ""

        for line in lines:
            line = line.strip()
            if not line or line.startswith("event:"):
                continue
            if line.startswith("data:"):
                payload = line[5:].strip()
            elif line.startswith("{"):
                payload = line
            else:
                continue
            if not payload:
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            c, r = self._parse_claude_event(obj)
            if c:
                content_parts.append(c)
            if r:
                reasoning_parts.append(r)

        return "".join(content_parts), "".join(reasoning_parts)

    # -- session URL -----------------------------------------------------

    def session_id_from_url(self, url: str) -> str | None:
        """Extract the conversation id from a claude.ai/chat/<id> URL."""
        m = _CHAT_ID_RE.search(url or "")
        return m.group(1) if m else None


class ClaudeScraperClient(BaseScraperConnector):
    """Connector wrapping the claude.ai headed-browser engine."""

    backend_name = "claude_scraper"
    provider_name = "claude"

    def _build_engine(
        self, account: str, browser_data_dir: str | None = None,
        *, adopt_from: "ClaudeScraperEngine | None" = None,
    ) -> ClaudeScraperEngine:
        from pathlib import Path
        from engine.config import _SYSTEM
        user_data_dir = Path(browser_data_dir) if browser_data_dir else _SYSTEM / account

        # Launch mode is a PER-PROVIDER setting (`claude_headless` in
        # system/settings.json), toggled from Settings → Account → Claude.
        # It must NOT read the global scraper-engine `headless` key — that one
        # belongs to the Qwen/DeepSeek engine and would silently force this
        # connector into whatever mode those engines happen to be in.
        _headless = claude_headless()
        _show_thoughts = True
        try:
            from engine.scraper import get_settings as _get_scraper_settings
            _show_thoughts = bool(_get_scraper_settings().get("show_thoughts", True))
        except Exception:
            pass

        if adopt_from is not None:
            return ClaudeScraperEngine(
                SPEC,
                viewer=not _headless,
                show_thoughts=_show_thoughts,
                user_data_dir=user_data_dir,
                profile_name=account,
                kill_existing=False,
                adopt_from=adopt_from,
            )
        return ClaudeScraperEngine(
            SPEC,
            viewer=not _headless,      # False -> --headless=new in the launcher
            show_thoughts=_show_thoughts,
            user_data_dir=user_data_dir,
            profile_name=account,
            kill_existing=True,
        )


_default_client: ClaudeScraperClient | None = None
_clients: dict[str, ClaudeScraperClient] = {}


def get_client(account: str | None = None) -> ClaudeScraperClient:
    """Factory mirroring the other connectors' get_client()."""
    global _default_client
    if account is None:
        if _default_client is None:
            _default_client = ClaudeScraperClient()
        return _default_client
    if account not in _clients:
        _clients[account] = ClaudeScraperClient(account=account)
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