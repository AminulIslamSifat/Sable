
"""Local model connector client — talks to OpenAI-compatible /v1/chat/completions."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from connectors.common.native_tools import (
    openai_to_openai_tools,
    format_openai_tool_result,
    native_call_to_tag_event,
    native_preview_lines,
)

logger = logging.getLogger("sable.connectors.local")

# Scalar timeout applies to connect/write/read/pool alike — a slow model
# generating a large tool-call `arguments` string (big create_file body)
# blows the 120s read clock and surfaces a bogus "model too large" error.
# SSE needs no per-chunk deadline: the server closes the stream when done.
_DEFAULT_TIMEOUT = httpx.Timeout(connect=15.0, read=None, write=120.0, pool=15.0)


class LocalConnector:
    """Connector for locally-served models via OpenAI-compatible API."""

    def __init__(self, endpoint: str = "http://127.0.0.1:8080/v1", api_key: str = "sable-local"):
        from connectors.common.error_ctx import ErrorContext
        self.ctx = ErrorContext("local")
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._available = True

    @property
    def is_available(self) -> bool:
        return self._available

    @property
    def endpoint(self) -> str:
        return self._endpoint

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def stream_chat(
        self,
        message: str,
        *,
        model: str | None = None,
        thinking_mode: str | None = None,
        chat_id: str | None = None,
        ref_file_ids: list[str] | None = None,
        inject_instructions: bool = True,
        system_prompt: str = "",
        history: list[dict[str, str]] | None = None,
        model_id: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Stream chat completion from local model."""
        # Pop db_history from kwargs (passed by chat route for session seeding)
        db_history = kwargs.pop("db_history", None)
        if db_history and not history:
            from connectors.common.history_budget import trim_history_messages
            history = trim_history_messages(db_history)

        # Native tool calling: extract tools from kwargs
        tools = kwargs.pop("tools", None)
        # Pop unused kwargs to keep things clean
        kwargs.pop("files", None)
        kwargs.pop("project_id", None)
        kwargs.pop("max_session_chars", None)

        # Build system prompt: explicit > cookbook settings > empty
        effective_prompt = system_prompt
        if not effective_prompt and inject_instructions:
            try:
                from engine.cookbook.model_settings import build_system_prompt
                # model_id may be the full "local/xxx" or just the label
                resolved_id = model_id or model or ""
                if resolved_id and not resolved_id.startswith("local/"):
                    resolved_id = f"local/{resolved_id.lower().replace(' ', '-')}"
                effective_prompt = build_system_prompt(resolved_id) or ""
            except Exception:
                pass

        messages = []
        if effective_prompt:
            messages.append({"role": "system", "content": effective_prompt})
        if history:
            messages.extend(history)
        # Avoid duplicating the current message if db_history already includes it
        if not (messages and messages[-1].get("role") == "user" and messages[-1].get("content") == message):
            messages.append({"role": "user", "content": message})

        # Determine thinking mode from parameter
        _enable_thinking = False
        if thinking_mode and thinking_mode != "fast":
            _enable_thinking = True

        payload = {
            "model": model or "default",
            "messages": messages,
            "stream": True,
            "temperature": kwargs.get("temperature", 0.7),
            "max_tokens": kwargs.get("max_tokens", 128000),
            "chat_template_kwargs": {"enable_thinking": _enable_thinking},
        }

        # Native tool calling: add tools to payload
        if tools:
            oai_tools = openai_to_openai_tools(tools)
            if oai_tools:
                payload["tools"] = oai_tools

        # Debug: dump full payload to log file
        try:
            from engine.config import OUTPUT_ROOT as _out_root
            import json as _json
            _log_file = _out_root / "local_model_payload.txt"
            _log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(_log_file, "a", encoding="utf-8") as _f:
                from datetime import datetime as _dt
                _f.write(f"\n{'='*80}\n")
                _f.write(f"TIMESTAMP: {_dt.now().isoformat()}\n")
                _f.write(f"MODEL: {model}\n")
                _f.write(f"ENDPOINT: {self._endpoint}\n")
                _f.write(f"MESSAGE COUNT: {len(messages)}\n")
                _f.write(f"{'='*80}\n")
                _f.write(_json.dumps(payload, indent=2, ensure_ascii=False))
                _f.write("\n")
        except Exception:
            pass

        # Tool execution loop
        _max_tool_rounds = 20
        _tool_round = 0

        while _tool_round < _max_tool_rounds:
            _tool_round += 1

            try:
                full_answer = ""
                async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
                    async with client.stream(
                        "POST",
                        f"{self._endpoint}/chat/completions",
                        json=payload,
                        headers=self._headers(),
                    ) as response:
                        if response.status_code != 200:
                            body = await response.aread()
                            yield self.ctx.error(f"HTTP {response.status_code}: {body.decode()[:200]}", status_code=response.status_code)
                            return

                        # Accumulate streaming tool call deltas
                        _tc_buffers: dict[int, dict] = {}
                        _pending_tag = ""
                        _last_progress = 0
                        # Provider-agnostic fallback: some llama-server builds
                        # emit reasoning as a native field, others embed it in
                        # content as <think>...</think>. Route content through
                        # the shared splitter so both paths become thinking.
                        from engine.think_parser import ThinkTagSplitter
                        _think_split = ThinkTagSplitter()

                        async for line in response.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            data = line[6:]
                            if data.strip() == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data)
                            except (ValueError, json.JSONDecodeError):
                                continue
                            delta = (chunk.get("choices") or [{}])[0].get("delta", {})

                            # Thinking / reasoning (native field from llama-server)
                            reasoning = delta.get("reasoning_content", "")
                            if reasoning:
                                yield {"type": "thinking", "text": reasoning}

                            # Text content — split any leaked <think> blocks out
                            content = delta.get("content", "")
                            if content and isinstance(content, str):
                                for _kind, _seg in _think_split.feed(content):
                                    if _kind == "thinking":
                                        yield {"type": "thinking", "text": _seg}
                                    else:
                                        full_answer += _seg
                                        yield {"type": "answer", "text": _seg}

                            # Tool call deltas (streaming)
                            tc_deltas = delta.get("tool_calls")
                            if tc_deltas:
                                for tcd in tc_deltas:
                                    idx = tcd.get("index", 0)
                                    if idx not in _tc_buffers:
                                        _tc_buffers[idx] = {"id": tcd.get("id", ""), "name": "", "args_str": ""}
                                    buf = _tc_buffers[idx]
                                    if tcd.get("id"):
                                        buf["id"] = tcd["id"]
                                    fn = tcd.get("function", {})
                                    if fn.get("name"):
                                        buf["name"] = fn["name"]
                                    if fn.get("arguments"):
                                        buf["args_str"] += fn["arguments"]

                                    # Live card — mirrors the XML/text path so a
                                    # native call renders while it streams instead
                                    # of popping in fully formed at the end.
                                    if buf["name"] and buf["name"] != _pending_tag:
                                        _pending_tag = buf["name"]
                                        _last_progress = 0
                                        yield {"type": "tool_pending", "tag": buf["name"], "attrs": {}}

                                    _nbytes = len(buf["args_str"].encode("utf-8"))
                                    if _nbytes and _nbytes - _last_progress >= 32:
                                        _last_progress = _nbytes
                                        _partial_args = None
                                        try:
                                            _partial_args = json.loads(buf["args_str"])
                                        except (ValueError, json.JSONDecodeError):
                                            pass
                                        yield {
                                            "type": "tool_progress",
                                            "tag": buf["name"],
                                            "bytes": _nbytes,
                                            "preview_lines": native_preview_lines(
                                                _partial_args, buf["args_str"], _nbytes
                                            ),
                                        }

                        # Check if we got tool calls
                        if _tc_buffers:
                            fc_calls = []
                            for idx in sorted(_tc_buffers.keys()):
                                buf = _tc_buffers[idx]
                                try:
                                    args = json.loads(buf["args_str"]) if buf["args_str"] else {}
                                except json.JSONDecodeError:
                                    args = {}
                                fc_calls.append({"name": buf["name"], "args": args, "id": buf["id"]})

                            if fc_calls:
                                # Save assistant message with tool_calls to history
                                assistant_msg: dict[str, Any] = {"role": "assistant", "content": full_answer or None}
                                assistant_msg["tool_calls"] = [
                                    {"id": fc["id"], "type": "function", "function": {"name": fc["name"], "arguments": json.dumps(fc["args"])}}
                                    for fc in fc_calls
                                ]
                                messages.append(assistant_msg)

                                # Execute each tool call
                                import asyncio as _asyncio
                                from engine.skills import get_skill_engine as _get_engine
                                engine = _get_engine()

                                for fc in fc_calls:
                                    tag_event = native_call_to_tag_event(fc)

                                    try:
                                        events = await _asyncio.to_thread(
                                            lambda: list(engine.process_tag(
                                                tag_event["name"], tag_event["attrs"],
                                                tag_event["content"], namespace="maria",
                                            ))
                                        )
                                    except Exception as exc:
                                        events = [{"type": "skill_end", "name": tag_event["name"], "ok": False, "error": str(exc)}]

                                    result_text = ""
                                    ok = True
                                    for evt in events:
                                        if evt.get("type") == "skill_output":
                                            result_text += evt.get("text", "")
                                        elif evt.get("type") == "skill_end":
                                            ok = evt.get("ok", True)
                                        yield evt

                                    tool_result = format_openai_tool_result(tag_event["name"], result_text, ok, fc.get("id", ""))
                                    messages.append(tool_result)
                                    logger.info("Native tool %s executed (ok=%s), continuing loop", tag_event["name"], ok)

                                # Update payload messages for next round
                                payload["messages"] = messages
                                continue  # Re-enter API call with tool results

                        # Normal completion (no tool calls)
                        messages.append({"role": "assistant", "content": full_answer})
                        yield {"type": "done", "parent_id": None}
                        return

            except httpx.ConnectError:
                yield self.ctx.error(f"cannot connect to {self._endpoint}. Is the server running?")
                return
            except httpx.TimeoutException:
                yield self.ctx.error("timed out. The model may be too large for available RAM.")
                return
            except Exception as exc:
                yield self.ctx.error(f"error: {exc}")
                return

    async def chat(
        self,
        message: str,
        *,
        model: str | None = None,
        thinking_mode: str | None = None,
        chat_id: str | None = None,
        ref_file_ids: list[str] | None = None,
        inject_instructions: bool = True,
        system_prompt: str = "",
        history: list[dict[str, str]] | None = None,
        model_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Non-streaming chat completion."""
        answer_parts = []
        error = None

        async for event in self.stream_chat(
            message,
            model=model,
            thinking_mode=thinking_mode,
            chat_id=chat_id,
            ref_file_ids=ref_file_ids,
            inject_instructions=inject_instructions,
            system_prompt=system_prompt,
            history=history,
            model_id=model_id,
            **kwargs,
        ):
            if event["type"] == "answer":
                answer_parts.append(event["text"])
            elif event["type"] == "error":
                error = event["message"]

        return {
            "answer": "".join(answer_parts),
            "thinking": "",
            "parent_id": None,
            "error": error,
        }


# Singleton per endpoint (keyed by endpoint URL)
_instances: dict[str, LocalConnector] = {}


def get_client(endpoint: str = "http://127.0.0.1:8080/v1", api_key: str = "sable-local") -> LocalConnector:
    """Get or create a LocalConnector for the given endpoint."""
    if endpoint not in _instances:
        _instances[endpoint] = LocalConnector(endpoint=endpoint, api_key=api_key)
    return _instances[endpoint]
