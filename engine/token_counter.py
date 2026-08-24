"""Lightweight token estimation for Sable.

Uses char/4 heuristic (standard approximation for English text with mixed
code/markdown). Accurate to ~10-15% for most LLM tokenizers. Applied
uniformly across all providers so dashboard numbers are comparable.

Counts include:
- System instructions / persona
- Tool schemas (JSON serialized)
- Conversation history (all messages in context window)
- User message
- Assistant response (completion side)
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Average chars per token across common LLM tokenizers.
# GPT-4/Claude: ~3.5-4, Qwen: ~3-4, DeepSeek: ~3.5-4.
# 4 is a safe conservative estimate.
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Estimate token count from text using char/4 heuristic."""
    if not text:
        return 0
    return max(1, len(text) // CHARS_PER_TOKEN)


def count_prompt_tokens(
    *,
    system_instruction: str = "",
    tools: list[dict[str, Any]] | None = None,
    history: list[dict[str, Any]] | None = None,
    user_message: str = "",
    memory_context: str = "",
    project_context: str = "",
) -> int:
    """Count estimated prompt tokens for a full LLM request.

    Includes everything sent to the model: instructions, tools, history,
    memory, project context, and the current user message.
    """
    total = 0

    # System instruction / persona
    if system_instruction:
        total += estimate_tokens(system_instruction)

    # Memory context injected into prompt
    if memory_context:
        total += estimate_tokens(memory_context)

    # Project context injected into prompt
    if project_context:
        total += estimate_tokens(project_context)

    # Tool schemas (serialized as JSON — models see the JSON structure)
    if tools:
        try:
            tools_json = json.dumps(tools, ensure_ascii=False)
            total += estimate_tokens(tools_json)
        except Exception:
            # Fallback: estimate from repr
            total += estimate_tokens(repr(tools))

    # Conversation history
    if history:
        for msg in history:
            content = msg.get("content", "")
            if isinstance(content, list):
                # Multimodal: sum text parts
                text_parts = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text_parts.append(part.get("text", ""))
                    elif isinstance(part, str):
                        text_parts.append(part)
                content = "\n".join(text_parts)
            elif not isinstance(content, str):
                content = str(content) if content else ""
            # Add role overhead (~2 tokens per message)
            total += 2 + estimate_tokens(content)

    # Current user message
    if user_message:
        total += 2 + estimate_tokens(user_message)

    return total


def count_completion_tokens(response_text: str, thinking_text: str = "") -> int:
    """Count estimated completion tokens (assistant response + thinking)."""
    total = 0
    if response_text:
        total += estimate_tokens(response_text)
    if thinking_text:
        total += estimate_tokens(thinking_text)
    return total
