
"""Local model connector client — talks to OpenAI-compatible /v1/chat/completions."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Any

import httpx

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
        messages.append({"role": "user", "content": message})

        payload = {
            "model": model or "default",
            "messages": messages,
            "stream": True,
            "temperature": kwargs.get("temperature", 0.7),
            "max_tokens": kwargs.get("max_tokens", 4096),
        }

        try:
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

                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data = line[6:]
                        if data.strip() == "[DONE]":
                            break
                        try:
                            import json
                            chunk = json.loads(data)
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                yield {"type": "answer", "text": content}
                        except (ValueError, IndexError, KeyError):
                            continue

            yield {"type": "done", "parent_id": None}

        except httpx.ConnectError:
            yield {"type": "error", "message": f"Cannot connect to local model at {self._endpoint}. Is the server running?"}
        except httpx.TimeoutException:
            yield {"type": "error", "message": "Local model timed out. The model may be too large for available RAM."}
        except Exception as exc:
            yield {"type": "error", "message": f"Local model error: {exc}"}

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
