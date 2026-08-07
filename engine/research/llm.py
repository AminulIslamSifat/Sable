
"""Stateless Qwen completion helper for the research engine.

Each research call spins up a fresh upstream chat session, streams one
completion, and returns the accumulated text.  Uses the shared ChatService
singleton for WAF headers (cached per-account tokens, browser fallback).
"""
from __future__ import annotations

import json
import logging

import httpx

logger = logging.getLogger("sable.research.llm")


def _strip_thinking(text: str) -> str:
    """Remove any <think>...</think> reasoning blocks from model output."""
    import re
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()


async def qwen_complete(
    prompt: str,
    model: str | None = None,
    timeout: int = 180,
    system_prefix: str = "",
    account: str | None = None,
) -> str:
    """Run a single stateless Qwen completion and return the full text.

    Args:
        prompt: The user-turn content.
        model: Model id (falls back to config default).
        timeout: Wall-clock seconds for the streamed completion.
        system_prefix: Optional text prepended to the prompt.
        account: Optional browser account name (e.g. "browser-data-acc2").
            When given, authenticates with that account's cached Qwen WAF
            tokens instead of the shared/active service headers. Raises if the
            account has no cached tokens or can't open a session, so callers
            can rotate to a fallback account.
    """
    logger.info("qwen_complete | model=%s account=%s timeout=%d prompt_len=%d",
                model, account, timeout, len(prompt))
    # Lazy import to avoid a server→engine→server circular import at module load.
    from server.api.dependencies import service
    from engine.config import URL, get_qwen_tokens_for_account
    from engine.payloads import build_body
    from engine.session import create_new_chat, build_headers

    full_prompt = (system_prefix + "\n\n" + prompt) if system_prefix else prompt

    # Account-specific tokens → dedicated headers; else shared service headers.
    if account:
        logger.debug("using account-specific tokens for %s", account)
        tok = get_qwen_tokens_for_account(account)
        if not (tok and tok.get("cookies")):
            logger.error("no cached Qwen tokens for account '%s'", account)
            raise RuntimeError(f"No cached Qwen tokens for account '{account}'")
        headers = build_headers(tok.get("cookies"), tok.get("bx_ua"), tok.get("bx_umidtoken"))
    else:
        logger.debug("using shared service headers")
        headers = await service._ensure_headers()

    logger.debug("creating new chat session | model=%s", model)
    chat_id = await create_new_chat(headers, model=model)
    if not chat_id:
        if account:
            logger.error("could not create Qwen session for account '%s'", account)
            # Don't silently fall back to the active account — surface the failure
            # so the research engine can rotate to the next fallback account.
            raise RuntimeError(f"Could not create Qwen session for account '{account}'")
        logger.warning("chat creation failed with shared headers, refreshing")
        headers = await service._refresh_headers()
        chat_id = await create_new_chat(headers, model=model)
    if not chat_id:
        logger.error("could not create Qwen chat session after refresh")
        raise RuntimeError("Could not create Qwen chat session for research")
    logger.info("chat session created | chat_id=%s", chat_id)

    # Fast mode (thinking disabled) for cheaper/faster structured extraction.
    body = build_body(full_prompt, chat_id, None, model=model, thinking_mode="fast")
    params = {"chat_id": chat_id}
    accumulated = ""

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15)) as client:
        async with client.stream("POST", URL, headers=headers, json=body, params=params) as resp:
            if resp.status_code in (401, 403):
                logger.error("qwen auth failed | status=%d account=%s", resp.status_code, account)
                raise RuntimeError(f"Qwen auth failed ({resp.status_code})")
            if resp.status_code != 200:
                raw = (await resp.aread()).decode(errors="replace")
                logger.error("qwen http error | status=%d body=%s", resp.status_code, raw[:300])
                raise RuntimeError(f"Qwen HTTP {resp.status_code}: {raw[:300]}")
            logger.debug("streaming response | status=%d", resp.status_code)
            buffer = ""
            async for chunk in resp.aiter_bytes():
                if not chunk:
                    continue
                buffer += chunk.decode("utf-8", errors="replace")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line.startswith("data: "):
                        continue
                    try:
                        data = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    choices = data.get("choices", [])
                    if not choices:
                        continue
                    content = choices[0].get("delta", {}).get("content", "")
                    if content:
                        accumulated += content

    result = _strip_thinking(accumulated).strip()
    logger.info("qwen_complete done | response_len=%d chat_id=%s", len(result), chat_id)
    return result


def extract_json(text: str) -> dict | list | None:
    """Best-effort JSON extraction from a model response (handles code fences)."""
    import re
    if not text:
        logger.debug("extract_json | empty input")
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    # Try the whole thing first
    try:
        result = json.loads(cleaned)
        logger.debug("extract_json | direct parse ok type=%s", type(result).__name__)
        return result
    except Exception:
        pass
    # Fall back to the first {...} or [...] block
    m = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", cleaned)
    if m:
        try:
            result = json.loads(m.group(1))
            logger.debug("extract_json | regex fallback ok type=%s", type(result).__name__)
            return result
        except Exception:
            logger.warning("extract_json | regex match found but invalid json")
            return None
    logger.warning("extract_json | no json found in response (len=%d)", len(text))
    return None
