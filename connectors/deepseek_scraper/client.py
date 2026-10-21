"""DeepSeek scraper connector — headed-browser DOM/SSE transport.

This is the *experimental*, per-model alternative to the pure-API DeepSeek
connector. It drives the real chat.deepseek.com SPA in a headed browser using
the same account profile the API ghost uses, and scrapes the DOM + taps the
in-page SSE stream instead of calling the API. Same login, different transport.

Selectable as its own model (``api_backend = "deepseek_scraper"``) — it is NOT
tied to the global scraper-mode toggle.

Current SPA layout (no model selector — DeepSeek unified all modes):
  * Two composer toggles: ``Search`` and ``DeepThink``. Both are
    ``div.ds-toggle-button`` and expose ``aria-pressed`` plus the
    ``ds-toggle-button--selected`` class when active.
  * Rules enforced here:
      - Search is ALWAYS forced off before every send.
      - DeepThink mirrors the chat's thinking mode, state-checked per message.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from connectors.common.browser_scraper import BaseBrowserEngine, ScraperSpec
from connectors.common.scraper_connector import BaseScraperConnector

SELECTORS: dict[str, str] = {
    "input": "textarea[name='search']",
    # Send button: up-arrow icon. The path prefix matches both the old
    # (M8.3125 0.981587) and new (M8.3125 0.980206) variants.
    "send_btn": "div[role='button']:has(path[d^='M8.3125 0.98'])",
    "response": "div.ds-message",
    "content": "div.ds-assistant-message-main-content",
    "stop": "div[role='button']:has(path[d^='M2 4.88'])",
    "thoughts": "div.ds-think-content",
    # Composer toggles (both are div.ds-toggle-button with a label span).
    "deepthink_toggle": "div.ds-toggle-button:has-text('DeepThink')",
    "search_toggle": "div.ds-toggle-button:has-text('Search')",
    # Attach (+) button — capsule icon button next to the composer.
    "attach_btn": "div[role='button'].ds-button--capsule:has(path[d^='M8 0.599609'])",
}

SPEC = ScraperSpec(
    provider="deepseek",
    url="https://chat.deepseek.com/",
    selectors=SELECTORS,
    keys={"send": "Enter"},
    sse_target="/api/v0/chat/completion",
    stealth_mode=True,
    instruction_provider="deepseek",
    # Soft refusals render as transcript siblings, not toasts. Verified live on
    # acc2 by rapid-firing messages until the limiter tripped:
    #   <span>Messages too frequent. Try again later.</span>
    # sibling of the last .ds-message, no SSE frame, empty assistant bubble.
    error_text_markers=(
        "Messages too frequent",
        "Too many requests",
        "Try again later",
        "rate limit",
        "usage limit",
    ),
)

# Values that mean "thinking on" for set_thinking_mode().
_THINKING_ON = ("deepthink", "thinking", "on", "true")

# DeepSeek chat URLs look like https://chat.deepseek.com/a/chat/s/<session_id>
_SESSION_ID_RE = re.compile(r"/a/chat/s/([^/?#]+)")


class DeepSeekScraperEngine(BaseBrowserEngine):
    """DeepSeek SPA driver: Search/DeepThink toggles + DeepSeek SSE delta parsing."""

    # -- SSE parsing -----------------------------------------------------

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # DeepSeek's patch-protocol state (reset per request).
        self._sse_frag_type: str = "RESPONSE"
        self._sse_line_buf: str = ""
        self._sse_seen_content: bool = False

    def _reset_sse_capture(self) -> None:
        super()._reset_sse_capture()
        self._sse_frag_type = "RESPONSE"
        self._sse_line_buf = ""
        self._sse_seen_content = False

    def _route_delta(self, text: str) -> tuple[str, str]:
        """Route a content delta to (answer, reasoning) based on fragment type.

        Matching is substring + case-insensitive on purpose: DeepSeek has
        shipped the reasoning fragment as ``THINK`` and (newer builds) as
        ``thinking`` / ``reasoning``. An exact ``== "THINK"`` check silently
        routed those reasoning deltas into the answer stream — the
        'thinking rendered as the model response' symptom.
        """
        if not text:
            return "", ""
        ftype = (self._sse_frag_type or "").upper()
        if "THINK" in ftype or "REASON" in ftype:
            return "", text
        return text, ""

    def _parse_deepseek_obj(self, obj: dict[str, Any]) -> tuple[str, str]:
        """Map one DeepSeek patch object to (content_delta, reasoning_delta).

        DeepSeek does NOT use the OpenAI ``choices[].delta`` envelope. The
        browser stream is a series of ``{"p": patch, "o": op, "v": value}``
        operations. Schema (verified against the API connector):

          * ``p=response/fragments o=APPEND``  → v=[{type, content}]  (first chunk)
          * ``p=response/fragments/-1/content``→ v=str               (incremental)
          * ``p=response/status  o=SET v=FINISHED`` → terminal (no content)
        """
        v = obj.get("v")
        p = obj.get("p")
        o = obj.get("o")

        # Patch protocol: a standalone type-set op for the current fragment.
        # DeepSeek occasionally announces the type before appending content; if
        # this is dropped the following content deltas inherit the PREVIOUS
        # fragment's type and reasoning leaks into the answer (or vice-versa).
        if isinstance(p, str) and p.endswith("/type") and isinstance(v, str):
            self._sse_frag_type = v
            return "", ""

        # Patch protocol: fragments append — carries the fragment type + first text.
        # MUST stay gated on ``o == "APPEND"`` and read only ``v[0]``.
        # DeepSeek re-sends the WHOLE fragments array on SET/RESET/metadata
        # ops; looping over every element (or accepting any op) re-emits all
        # prior fragments on each event, which is the Nx content duplication.
        if p == "response/fragments" and o == "APPEND" and isinstance(v, list) and v:
            frag = v[0] if isinstance(v[0], dict) else {}
            self._sse_frag_type = (
                frag.get("type")
                or frag.get("fragment_type")
                or self._sse_frag_type
            )
            return self._route_delta(frag.get("content", "") or "")

        # Patch protocol: incremental content on the current fragment.
        if p == "response/fragments/-1/content":
            return self._route_delta(v if isinstance(v, str) else "")

        # Metadata event carrying a full response with fragments.
        if isinstance(v, dict) and "response" in v:
            out_c: list[str] = []
            out_r: list[str] = []
            for frag in (v["response"] or {}).get("fragments", []) or []:
                self._sse_frag_type = frag.get("type", "RESPONSE")
                cc, rr = self._route_delta(frag.get("content", "") or "")
                if cc:
                    out_c.append(cc)
                if rr:
                    out_r.append(rr)
            return "".join(out_c), "".join(out_r)

        # Bare string delta with no patch metadata.
        if isinstance(v, str) and p is None:
            return self._route_delta(v)

        # Legacy OpenAI-style fallback (in case DeepSeek ever serves it).
        try:
            delta = obj.get("choices", [{}])[0].get("delta", {})
            return (delta.get("content", "") or "", delta.get("reasoning_content", "") or "")
        except (IndexError, KeyError, TypeError):
            return "", ""

    def parse_sse_delta(self, raw_text: str) -> tuple[str, str]:
        """Parse DeepSeek's patch-protocol SSE stream into (content, reasoning).

        Lines can be split across network reads, so a partial trailing line is
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
            c, r = self._parse_deepseek_obj(obj)
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
        """Extract the DeepSeek chat session id from a chat.deepseek.com URL."""
        m = _SESSION_ID_RE.search(url or "")
        return m.group(1) if m else None

    # -- toggle helpers --------------------------------------------------

    async def _toggle_is_on(self, toggle: Any) -> bool:
        """Read a ds-toggle-button's live on/off state.

        Prefers ``aria-pressed`` (always present); falls back to the
        ``ds-toggle-button--selected`` class.
        """
        try:
            pressed = await toggle.get_attribute("aria-pressed")
            if pressed is not None:
                return pressed == "true"
            classes = await toggle.get_attribute("class") or ""
            return "ds-toggle-button--selected" in classes
        except Exception:
            return False

    async def _set_toggle(self, selector: str, want_on: bool, *, label: str = "") -> bool:
        """Idempotently set a toggle to `want_on`. Returns True if it changed."""
        if not selector:
            return False
        try:
            toggle = self.page.locator(selector).first
            if not await toggle.is_visible(timeout=2500):
                return False
            is_on = await self._toggle_is_on(toggle)
            if is_on == want_on:
                return False
            await toggle.click()
            await asyncio.sleep(0.4)
            self._log("toggle_set", label=label, want=want_on)
            return True
        except Exception as exc:
            self._log("toggle_set_failed", label=label, error=str(exc))
            return False

    async def _ensure_search_off(self) -> None:
        """Force the Search toggle OFF. Called immediately before every send."""
        await self._set_toggle(self.spec.sel("search_toggle"), False, label="search")

    async def set_thinking_mode(self, mode: str) -> None:
        """Mirror the Sable thinking mode onto DeepSeek's DeepThink toggle.

        State-checked on every call, so it is idempotent per message.
        """
        want_on = str(mode or "").lower() in _THINKING_ON
        await self._set_toggle(self.spec.sel("deepthink_toggle"), want_on, label="deepthink")

    # -- send ------------------------------------------------------------

    async def send_msg(self, message: str, raw: bool = False) -> bool:
        # Search must never be on — enforce it right before sending.
        await self._ensure_search_off()
        return await super().send_msg(message, raw=raw)

    async def _send_hello_warmup(self) -> None:
        """Seed a bare 'hello' so the first instruction-heavy turn chains cleanly."""
        try:
            await self._ensure_search_off()
            field = await self._find_input_field()
            if field is None:
                return
            await field.click()
            filled = await self._paste_large_message(field, "hello")
            if not filled:
                await field.fill("hello")
            await self._wait_for_send_enabled()
            await self._ensure_fetch_patched()
            self._reset_sse_capture()

            send_sel = self.spec.sel("send_btn")
            sent = False
            if send_sel:
                try:
                    btn = self.page.locator(send_sel).first
                    classes = (await btn.get_attribute("class") or "").lower()
                    if "disabled" not in classes:
                        await btn.click()
                        sent = True
                except Exception:
                    pass
            if not sent:
                await self.page.keyboard.press(self.spec.keys.get("send", "Enter"))
            try:
                await self.get_response()
            except Exception:
                pass
            self._reset_sse_capture()
        except Exception:
            pass


class DeepSeekScraperClient(BaseScraperConnector):
    """Connector wrapping the DeepSeek headed-browser engine."""

    backend_name = "deepseek_scraper"
    provider_name = "deepseek"

    def _build_engine(
        self, account: str, browser_data_dir: str | None = None,
        *, adopt_from: "DeepSeekScraperEngine | None" = None,
    ) -> DeepSeekScraperEngine:
        # Honor the account this connector was resolved for (NOT the globally
        # active one). The profile dir and the browser binary both key off the
        # account name, so a pinned-account client opens the right profile +
        # the right browser (Helium/Chrome/Edge per accounts.json).
        #
        # When browser_data_dir is pinned (critique/agent sub-chats) use that
        # profile dir verbatim so the sub-chat gets a real isolated session
        # instead of silently sharing the parent chat's browser.
        from pathlib import Path
        from engine.config import _SYSTEM
        user_data_dir = Path(browser_data_dir) if browser_data_dir else _SYSTEM / account
        if adopt_from is not None:
            # Share the parent's live browser on a new tab. user_data_dir is
            # inherited via the parent's context — no second launch, no
            # kill_existing (which would murder the parent browser).
            return DeepSeekScraperEngine(
                SPEC,
                viewer=True,
                show_thoughts=True,
                user_data_dir=user_data_dir,
                profile_name=account,
                kill_existing=False,
                adopt_from=adopt_from,
            )
        # Headless is user-controlled from Settings → Account → DeepSeek
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
        return DeepSeekScraperEngine(
            SPEC,
            viewer=not _headless,      # False -> --headless=new in the launcher
            show_thoughts=_show_thoughts,
            user_data_dir=user_data_dir,
            profile_name=account,
            kill_existing=True,        # kill prior browsers on this profile before opening
        )


_default_client: DeepSeekScraperClient | None = None
_clients: dict[str, DeepSeekScraperClient] = {}


def get_client(account: str | None = None) -> DeepSeekScraperClient:
    """Factory mirroring the other connectors' get_client()."""
    global _default_client
    if account is None:
        if _default_client is None:
            _default_client = DeepSeekScraperClient()
        return _default_client
    if account not in _clients:
        _clients[account] = DeepSeekScraperClient(account=account)
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
