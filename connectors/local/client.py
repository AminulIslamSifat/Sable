
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
)

logger = logging.getLogger("sable.connectors.local")

_DEFAULT_TIMEOUT = 120.0


class LocalConnector:
    """Connector for locally-served models via OpenAI-compatible API."""

    def __init__(self, endpoint: str = "http://127.0.0.1:8080/v1", api_key: str = "sable-local"):
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
            history = db_history

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
                            yield {"type": "error", "message": f"Local model error ({response.status_code}): {body.decode()[:200]}"}
                            return

                        # Accumulate streaming tool call deltas
                        _tc_buffers: dict[int, dict] = {}
                        # Fallback: parse <think> tags from content when
                        # llama-server doesn't separate reasoning_content
                        _in_think = False
                        _got_reasoning_field = False
                        _tag_buf = ""  # partial tag accumulator

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
                                _got_reasoning_field = True
                                yield {"type": "thinking", "text": reasoning}

                            # Text content
                            content = delta.get("content", "")
                            if content and not _got_reasoning_field and _enable_thinking:
                                # Fallback: parse <think>...</think> from content
                                _answer_out = ""
                                _think_out = ""
                                _target_tag = "</think>" if _in_think else "<think>"
                                for ch in content:
                                    if _tag_buf:
                                        _tag_buf += ch
                                        if _target_tag.startswith(_tag_buf):
                                            if _tag_buf == _target_tag:
                                                _in_think = not _in_think
                                                _tag_buf = ""
                                                _target_tag = "</think>" if _in_think else "<think>"
                                            continue
                                        else:
                                            # Not a tag — flush buffer
                                            if _in_think:
                                                _think_out += _tag_buf
                                            else:
                                                _answer_out += _tag_buf
                                            _tag_buf = ""
                                            # Re-process current char
                                            if ch == "<":
                                                _tag_buf = "<"
                                            elif _in_think:
                                                _think_out += ch
                                            else:
                                                _answer_out += ch
                                            continue
                                    if ch == "<":
                                        _tag_buf = "<"
                                        continue
                                    if _in_think:
                                        _think_out += ch
                                    else:
                                        _answer_out += ch
                                # Flush any remaining tag buffer
                                if _tag_buf:
                                    if _in_think:
                                        _think_out += _tag_buf
                                    else:
                                        _answer_out += _tag_buf
                                    _tag_buf = ""
                                if _think_out:
                                    yield {"type": "thinking", "text": _think_out}
                                if _answer_out:
                                    full_answer += _answer_out
                                    yield {"type": "answer", "text": _answer_out}
                            elif content:
                                full_answer += content
                                yield {"type": "answer", "text": content}

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
                yield {"type": "error", "message": f"Cannot connect to local model at {self._endpoint}. Is the server running?"}
                return
            except httpx.TimeoutException:
                yield {"type": "error", "message": "Local model timed out. The model may be too large for available RAM."}
                return
            except Exception as exc:
                yield {"type": "error", "message": f"Local model error: {exc}"}
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
