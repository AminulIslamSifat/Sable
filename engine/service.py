"""FastAPI-friendly service layer for Sable chat engine."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import httpx

from engine.config import (
    URL,
    get_qwen_tokens_for_account,
    save_qwen_tokens_for_account,
    mark_account_exhausted,
)
from engine.payloads import build_body
from engine.session import BrowserManager, create_new_chat

logger = logging.getLogger("sable")



class ChatService:
    """Wraps the existing Sable engine into an async, coroutine-safe API-friendly layer.

    NOTE: BrowserManager and create_new_chat (session.py) are async (Playwright's
    async API), so every method here that touches them is async too. Callers
    (e.g. FastAPI route handlers) need `await service.foo()` and
    `async for event in service.stream_events(...)` instead of the old sync calls.
    """

    def __init__(self, user_data_dir: str | None = None) -> None:
        if user_data_dir is None:
            from engine.config import BROWSER_DATA_DIR
            user_data_dir = str(BROWSER_DATA_DIR)
        self._browser = BrowserManager(user_data_dir=user_data_dir)
        self._headers: dict[str, str] | None = None
        # Which account self._headers belong to (guards against fast double-switch
        # races where a stale in-memory header set outlives the active symlink).
        self._headers_account: str | None = None
        self._lock = asyncio.Lock()
        # Derive account name from user_data_dir for token lookup
        # e.g. ".../system/browser-data-acc3" → "browser-data-acc3"
        import re
        basename = Path(user_data_dir).name
        if re.match(r"browser-data-acc\d+$", basename):
            self._account_override: str | None = basename
        else:
            self._account_override = None

    def _mark_exhausted(self) -> None:
        """Mark the current account as quota-exhausted."""
        from engine.config import _resolve_active_account
        account = self._account_override or _resolve_active_account()
        mark_account_exhausted(account)

    async def close(self) -> None:
        async with self._lock:
            await self._browser.close()
            self._headers = None
            self._headers_account = None

    async def restart_browser(self, headless: bool | None = None) -> None:
        async with self._lock:
            await self._browser.restart(headless=headless)
            self._headers = None
            self._headers_account = None

    @property
    def browser_headless(self) -> bool:
        return self._browser.headless

    async def _ensure_headers(self) -> dict[str, str]:
        from engine.config import _resolve_active_account
        account = self._account_override or _resolve_active_account()
        # Fast path: headers for THIS account already in memory
        if self._headers and self._headers_account == account:
            return self._headers
        # Medium path: check per-account token cache before launching browser
        cached = get_qwen_tokens_for_account(account)
        if cached and cached.get("cookies"):
            from engine.session import build_headers
            self._headers = build_headers(
                cookies=cached["cookies"],
                bx_ua=cached.get("bx_ua"),
                bx_umidtoken=cached.get("bx_umidtoken"),
            )
            self._headers_account = account
            logger.info("Loaded cached Qwen WAF tokens for %s", account)
            return self._headers
        # Slow path: launch browser to fetch fresh headers
        async with self._lock:
            if not self._headers or self._headers_account != account:
                await self._browser.start()
                try:
                    self._headers = await self._browser.get_fresh_headers()
                    self._headers_account = account
                    # Save to per-account cache
                    save_qwen_tokens_for_account(
                        cookies=self._headers.get("Cookie", ""),
                        bx_ua=self._headers.get("bx-ua", ""),
                        bx_umidtoken=self._headers.get("bx-umidtoken", ""),
                        account=account,
                    )
                finally:
                    await self._browser.close()
            return self._headers

    async def _refresh_headers(self) -> dict[str, str]:
        async with self._lock:
            await self._browser.start()
            try:
                self._headers = await self._browser.get_fresh_headers()
                # Save refreshed tokens to per-account cache
                from engine.config import _resolve_active_account
                account = self._account_override or _resolve_active_account()
                self._headers_account = account
                save_qwen_tokens_for_account(
                    cookies=self._headers.get("Cookie", ""),
                    bx_ua=self._headers.get("bx-ua", ""),
                    bx_umidtoken=self._headers.get("bx-umidtoken", ""),
                    account=account,
                )
            finally:
                await self._browser.close()
            return self._headers

    async def warmup(self, account: str | None = None) -> None:
        """Pre-load WAF headers. Never launches a browser when the target
        account's tokens are cached on disk.

        Pass account= to pin the target — background callers MUST, since the
        active-profile symlink can move between scheduling and execution.
        """
        from engine.config import _resolve_active_account
        account = account or self._account_override or _resolve_active_account()
        # Fast path: headers for this account already in memory
        if self._headers and self._headers_account == account:
            return
        # Medium path: per-account token cache on disk — no browser launch needed.
        # Mirrors _ensure_headers(); if cached tokens turn out stale,
        # _refresh_headers() self-heals on first chat.
        cached = get_qwen_tokens_for_account(account)
        if cached and cached.get("cookies"):
            from engine.session import build_headers
            self._headers = build_headers(
                cookies=cached["cookies"],
                bx_ua=cached.get("bx_ua"),
                bx_umidtoken=cached.get("bx_umidtoken"),
            )
            self._headers_account = account
            logger.info("Warmup: loaded cached Qwen WAF tokens for %s (no browser launch)", account)
            return
        # Slow path: launch browser to fetch fresh headers
        async with self._lock:
            try:
                await self._browser.start()
                if not self._headers or self._headers_account != account:
                    self._headers = await self._browser.get_fresh_headers()
                    self._headers_account = account
                    # Persist WAF tokens to per-account cache
                    save_qwen_tokens_for_account(
                        cookies=self._headers.get("Cookie", ""),
                        bx_ua=self._headers.get("bx-ua", ""),
                        bx_umidtoken=self._headers.get("bx-umidtoken", ""),
                        account=account,
                    )
            except Exception as exc:
                logger.warning("Warmup failed: %s: %s", type(exc).__name__, exc)
                self._headers = None
                self._headers_account = None
            # Browser stays open — warm session for the first message; the next
            # switch's service.close() tears it down.

    async def refresh_deepseek_token(self) -> str:
        """Extract a fresh DeepSeek token. Reuses an already-running browser
        (e.g. one a cold warmup left open); closes it only if this call
        launched it."""
        async with self._lock:
            opened_here = not self._browser.is_running
            await self._browser.start()
            try:
                return await self._browser.extract_deepseek_token()
            finally:
                if opened_here:
                    await self._browser.close()

    async def create_chat(self, model: str | None = None) -> str | None:
        headers = await self._ensure_headers()
        chat_id = await create_new_chat(headers, model=model)
        if not chat_id:
            headers = await self._refresh_headers()
            chat_id = await create_new_chat(headers, model=model)
        return chat_id

    async def upload_image(self, image_path: str) -> dict[str, Any] | None:
        headers = await self._ensure_headers()
        return await self._browser.upload_image(
            image_path,
            cookies=headers.get("Cookie"),
            bx_ua=headers.get("bx-ua"),
            bx_umidtoken=headers.get("bx-umidtoken"),
        )

    async def upload_deepseek_file(
        self,
        file_path: str,
        model_type: str = "vision",
        thinking_enabled: bool = False,
    ) -> dict[str, Any]:
        """Upload a file for DeepSeek Vision via pure httpx (no browser)."""
        from connectors.deepseek.upload import upload_file

        return await upload_file(
            file_path,
            model_type=model_type,
            thinking_enabled=thinking_enabled,
        )

    async def sync_context(self, project_id: str | None = None) -> bool:
        # Reuse cached headers from warmup to avoid a redundant browser launch
        if self._headers:
            return await self._browser.sync_context(headers=self._headers, project_id=project_id)
        return await self._browser.sync_context(project_id=project_id)

    async def stream_events(
        self,
        message: str,
        chat_id: str | None = None,
        parent_id: str | None = None,
        files: list[dict[str, Any]] | None = None,
        model: str | None = None,
        thinking_mode: str | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        try:
            headers = await self._ensure_headers()
            active_chat_id = chat_id

            if not active_chat_id:
                active_chat_id = await create_new_chat(headers, model=model)
                if not active_chat_id:
                    headers = await self._refresh_headers()
                    active_chat_id = await create_new_chat(headers, model=model)

            if not active_chat_id:
                yield {"type": "error", "message": "Could not create chat session"}
                return
        except Exception as exc:
            yield {"type": "error", "message": f"Session startup failed: {type(exc).__name__}: {exc}"}
            return

        yield {"type": "meta", "chat_id": active_chat_id, "parent_id": parent_id}
        yield {"type": "status", "message": "calling_upstream"}

        body = build_body(message, active_chat_id, parent_id, files=files, model=model, thinking_mode=thinking_mode)
        params = {"chat_id": active_chat_id}

        try:
            async for event in self._stream_request(
                headers=headers,
                body=body,
                params=params,
                chat_id=active_chat_id,
                parent_id=parent_id,
                files=files,
                is_retry=False,
            ):
                yield event
        except httpx.ConnectError as exc:
            yield {"type": "error", "message": f"Connection failed: {exc}"}
        except httpx.ReadTimeout:
            yield {"type": "error", "message": "Timed out waiting for response"}
        except Exception as exc:
            yield {"type": "error", "message": f"{type(exc).__name__}: {exc}"}

    async def _stream_request(
        self,
        headers: dict[str, str],
        body: dict[str, Any],
        params: dict[str, str],
        chat_id: str,
        parent_id: str | None,
        files: list[dict[str, Any]] | None,
        is_retry: bool,
    ) -> AsyncGenerator[dict[str, Any], None]:
        max_attempts = 3
        last_error_msg: str | None = None

        for attempt in range(1, max_attempts + 1):
            new_parent_id = parent_id
            chosen_response_id: str | None = None
            got_content = False
            _thinking_sent_count = 0  # track cumulative thinking paragraphs already yielded
            status_code = 0
            needs_refresh = False

            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    async with client.stream("POST", URL, headers=headers, json=body, params=params) as res:
                        status_code = res.status_code
                        logger.debug("Upstream HTTP %s (attempt %d/%d)", res.status_code, attempt, max_attempts)
                        yield {"type": "debug", "message": f"HTTP {res.status_code} (attempt {attempt}/{max_attempts})"}

                        if res.status_code in (401, 403):
                            await res.aread()
                            needs_refresh = True
                        elif res.status_code != 200:
                            raw = (await res.aread()).decode(errors="replace")
                            # Check if non-200 response is actually a rate-limit or API error
                            try:
                                err_data = json.loads(raw)
                                if err_data.get("success") is False:
                                    inner = err_data.get("data", {})
                                    code = inner.get("code", "")
                                    if code == "RateLimited":
                                        hours = inner.get("num", "?")
                                        details = inner.get("details", "Daily usage limit reached.")
                                        self._mark_exhausted()
                                        yield {
                                            "type": "rate_limited",
                                            "message": details,
                                            "hours": hours,
                                            "template": inner.get("template", ""),
                                        }
                                        return
                                    if code == "CHAT_NOT_FOUND":
                                        yield {
                                            "type": "chat_not_found",
                                            "message": f"Upstream session expired: {inner.get('details', '')}",
                                        }
                                        return
                                    if code == "PARENT_NOT_FOUND":
                                        yield {
                                            "type": "parent_not_found",
                                            "message": f"Stale parent_id: {inner.get('details', '')}",
                                        }
                                        return
                                    yield {
                                        "type": "error",
                                        "message": f"API error [{code}]: {inner.get('details', 'Unknown error')}",
                                    }
                                    return
                            except (json.JSONDecodeError, ValueError):
                                pass
                            last_error_msg = f"HTTP {res.status_code}: {raw[:500]}"
                            continue
                        else:
                            buffer = ""
                            _total_bytes = 0
                            _chunk_count = 0
                            _answer_chars = 0
                            _finish_reason = None
                            _last_sse_data = None
                            logger.info(
                                "Qwen stream started: chat_id=%s attempt=%d model=%s",
                                chat_id, attempt, body.get("model", "?"),
                            )
                            async for chunk in res.aiter_bytes():
                                if not chunk:
                                    continue

                                _total_bytes += len(chunk)
                                _chunk_count += 1
                                buffer += chunk.decode("utf-8", errors="replace")
                                while "\n" in buffer:
                                    line, buffer = buffer.split("\n", 1)
                                    line = line.strip()

                                    if not line.startswith("data: "):
                                        # Check for non-SSE JSON error responses (e.g. rate limit)
                                        if line:
                                            try:
                                                err_data = json.loads(line)
                                                if err_data.get("success") is False:
                                                    inner = err_data.get("data", {})
                                                    code = inner.get("code", "")
                                                    if code == "RateLimited":
                                                        hours = inner.get("num", "?")
                                                        details = inner.get("details", "Daily usage limit reached.")
                                                        self._mark_exhausted()
                                                        yield {
                                                            "type": "rate_limited",
                                                            "message": details,
                                                            "hours": hours,
                                                            "template": inner.get("template", ""),
                                                        }
                                                        return
                                                    if code == "CHAT_NOT_FOUND":
                                                        yield {
                                                            "type": "chat_not_found",
                                                            "message": f"Upstream session expired: {inner.get('details', '')}",
                                                        }
                                                        return
                                                    if code == "PARENT_NOT_FOUND":
                                                        yield {
                                                            "type": "parent_not_found",
                                                            "message": f"Stale parent_id: {inner.get('details', '')}",
                                                        }
                                                        return
                                                    # Other API errors
                                                    yield {
                                                        "type": "error",
                                                        "message": f"API error [{code}]: {inner.get('details', 'Unknown error')}",
                                                    }
                                                    return
                                            except json.JSONDecodeError:
                                                pass
                                        continue

                                    try:
                                        data = json.loads(line[6:])
                                        _last_sse_data = data
                                    except json.JSONDecodeError:
                                        logger.warning(
                                            "Qwen SSE JSON parse failed: %s",
                                            line[:200],
                                        )
                                        continue

                                    # Capture finish_reason from choices or top-level
                                    _fr = None
                                    if data.get("choices"):
                                        _fr = data["choices"][0].get("finish_reason")
                                    if not _fr:
                                        _fr = data.get("finish_reason")
                                    if _fr:
                                        _finish_reason = _fr

                                    created = data.get("response.created")
                                    if isinstance(created, dict):
                                        response_id = created.get("response_id")
                                        if isinstance(response_id, str):
                                            if created.get("response_index") == "0" or chosen_response_id is None:
                                                chosen_response_id = response_id
                                                new_parent_id = response_id

                                    choices = data.get("choices", [])
                                    if not choices:
                                        continue

                                    response_id = data.get("response_id")
                                    if isinstance(response_id, str):
                                        if chosen_response_id is None:
                                            chosen_response_id = response_id
                                            new_parent_id = response_id
                                        elif response_id != chosen_response_id:
                                            continue

                                    delta = choices[0].get("delta", {})
                                    phase = delta.get("phase", "")
                                    content = delta.get("content", "")
                                    extra = delta.get("extra", {})

                                    tool_calls = delta.get("tool_calls") or extra.get("tool_calls")
                                    if tool_calls:
                                        yield {"type": "tool_call", "data": tool_calls}

                                    tool_results = delta.get("tool_results") or extra.get("tool_results")
                                    if tool_results:
                                        yield {"type": "tool_result", "data": tool_results}

                                    if phase in ("thinking_summary", "thinking"):
                                        thoughts = extra.get("summary_thought", {}).get("content", [])
                                        if thoughts:
                                            # API sends cumulative array; only yield new paragraphs
                                            new_parts = thoughts[_thinking_sent_count:]
                                            _thinking_sent_count = len(thoughts)
                                            if new_parts:
                                                got_content = True
                                                yield {"type": "thinking", "text": "\n\n".join(new_parts)}
                                        elif content:
                                            got_content = True
                                            yield {"type": "thinking", "text": content}
                                    elif phase == "answer" and content:
                                        got_content = True
                                        _answer_chars += len(content)
                                        yield {"type": "answer", "text": content}
                                    elif content:
                                        got_content = True
                                        _answer_chars += len(content)
                                        yield {"type": "answer", "text": content}

                            # Stream ended — log diagnostics
                            logger.info(
                                "Qwen stream ended: chat_id=%s attempt=%d "
                                "chunks=%d bytes=%d answer_chars=%d "
                                "finish_reason=%s got_content=%s "
                                "buffer_leftover=%d last_data_keys=%s",
                                chat_id, attempt, _chunk_count, _total_bytes,
                                _answer_chars, _finish_reason, got_content,
                                len(buffer), list(_last_sse_data.keys()) if _last_sse_data else "none",
                            )
                            if not got_content and _chunk_count > 0:
                                logger.warning(
                                    "Qwen stream produced %d chunks (%d bytes) but ZERO content. "
                                    "finish_reason=%s buffer_tail=%s",
                                    _chunk_count, _total_bytes, _finish_reason,
                                    buffer[-500:] if buffer else "(empty)",
                                )
                            if got_content and _finish_reason and _finish_reason != "stop":
                                logger.warning(
                                    "Qwen stream finished with reason=%s (not 'stop'). "
                                    "Response likely truncated. answer_chars=%d",
                                    _finish_reason, _answer_chars,
                                )

            except httpx.ConnectError as exc:
                last_error_msg = f"Connection failed: {exc}"
                continue
            except httpx.ReadTimeout:
                last_error_msg = "Timed out waiting for response"
                continue
            except Exception as exc:
                last_error_msg = f"{type(exc).__name__}: {exc}"
                continue

            if got_content:
                yield {"type": "done", "chat_id": chat_id, "parent_id": new_parent_id}
                return

            if needs_refresh:
                last_error_msg = f"HTTP {status_code} — auth rejected"

            if not got_content and not needs_refresh:
                # Check if buffer has leftover non-SSE JSON (e.g. rate-limit on HTTP 200)
                leftover = buffer.strip() if buffer else ""
                if leftover:
                    try:
                        err_data = json.loads(leftover)
                        if err_data.get("success") is False:
                            inner = err_data.get("data", {})
                            code = inner.get("code", "")
                            if code == "RateLimited":
                                hours = inner.get("num", "?")
                                details = inner.get("details", "Daily usage limit reached.")
                                self._mark_exhausted()
                                yield {
                                    "type": "rate_limited",
                                    "message": details,
                                    "hours": hours,
                                    "template": inner.get("template", ""),
                                }
                                return
                            if code == "CHAT_NOT_FOUND":
                                yield {
                                    "type": "chat_not_found",
                                    "message": f"Upstream session expired: {inner.get('details', '')}",
                                }
                                return
                            if code == "PARENT_NOT_FOUND":
                                yield {
                                    "type": "parent_not_found",
                                    "message": f"Stale parent_id: {inner.get('details', '')}",
                                }
                                return
                            yield {
                                "type": "error",
                                "message": f"API error [{code}]: {inner.get('details', 'Unknown error')}",
                            }
                            return
                    except (json.JSONDecodeError, ValueError):
                        pass
                last_error_msg = f"Upstream returned HTTP {status_code} with zero content — WAF tokens may be stale or the session expired"

            if attempt < max_attempts:
                logger.warning("Attempt %d failed: %s. Refreshing headers and retrying...", attempt, last_error_msg)
                yield {"type": "status", "message": f"retrying_attempt_{attempt + 1}"}
                yield {"type": "debug", "message": f"Attempt {attempt} failed: {last_error_msg}. Refreshing session."}
                headers = await self._refresh_headers()
                await asyncio.sleep(1 * attempt)
                continue

        yield {"type": "error", "message": f"Failed after {max_attempts} attempts: {last_error_msg}"}

    async def chat(
        self,
        message: str,
        chat_id: str | None = None,
        parent_id: str | None = None,
        files: list[dict[str, Any]] | None = None,
        model: str | None = None,
        thinking_mode: str | None = None,
    ) -> dict[str, Any]:
        thinking_parts: list[str] = []
        answer_parts: list[str] = []
        tool_events: list[dict[str, Any]] = []
        final_chat_id = chat_id
        final_parent_id = parent_id
        error: str | None = None

        async for event in self.stream_events(message, chat_id, parent_id, files, model=model, thinking_mode=thinking_mode):
            event_type = event.get("type")

            if event_type == "thinking":
                thinking_parts.append(str(event.get("text", "")))
            elif event_type == "answer":
                answer_parts.append(str(event.get("text", "")))
            elif event_type in ("tool_call", "tool_result"):
                tool_events.append(event)
            elif event_type == "meta":
                final_chat_id = event.get("chat_id") or final_chat_id
            elif event_type == "done":
                final_chat_id = event.get("chat_id") or final_chat_id
                final_parent_id = event.get("parent_id") or final_parent_id
            elif event_type == "chat_not_found":
                # Upstream session expired — create new one, inject history, retry once
                from engine.session import create_new_chat as _cnc
                _hdrs = await self._ensure_headers()
                _new_id = await _cnc(_hdrs, model=model)
                if not _new_id:
                    _hdrs = await self._refresh_headers()
                    _new_id = await _cnc(_hdrs, model=model)
                if _new_id:
                    # Re-call with new session, no history injection (non-streaming is simpler)
                    async for _evt in self.stream_events(message, _new_id, None, files, model=model, thinking_mode=thinking_mode):
                        _t = _evt.get("type")
                        if _t == "thinking":
                            thinking_parts.append(str(_evt.get("text", "")))
                        elif _t == "answer":
                            answer_parts.append(str(_evt.get("text", "")))
                        elif _t in ("tool_call", "tool_result"):
                            tool_events.append(_evt)
                        elif _t == "meta":
                            final_chat_id = _evt.get("chat_id") or final_chat_id
                        elif _t == "done":
                            final_chat_id = _evt.get("chat_id") or final_chat_id
                            final_parent_id = _evt.get("parent_id") or final_parent_id
                        elif _t == "error":
                            error = str(_evt.get("message", "Unknown error"))
                else:
                    error = "Failed to recover: could not create new upstream session"
            elif event_type == "error":
                error = str(event.get("message", "Unknown error"))

        return {
            "chat_id": final_chat_id,
            "parent_id": final_parent_id,
            "thinking": "".join(thinking_parts),
            "answer": "".join(answer_parts),
            "tool_events": tool_events,
            "error": error,
        }