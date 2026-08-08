
"""Proactive context compression for non-Qwen models.

Two-tier system:
  - 75% of max_session_chars → inject system hint, let model trigger <summarize_before>
  - 90% of max_session_chars → auto-summarize oldest 50% of messages

The summarizer uses the CURRENT active model (best judge of relevance).
Qwen models are excluded (4M char window doesn't need this).
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("sable.context_summarizer")

# Tag the model can emit to request summarization
_SUMMARIZE_TAG_RE = re.compile(
    r"<summarize_before\s+message_index=\"?(\d+)\"?\s*/>",
    re.IGNORECASE | re.DOTALL,
)

# Thresholds as fraction of max_session_chars
HINT_THRESHOLD = 0.75   # 75% → inject hint for model
FORCE_THRESHOLD = 0.90  # 90% → auto-summarize

# Summary prompt templates
_HINT_INJECTION = (
    "\n\n[SYSTEM NOTE: Context usage is at {pct:.0f}% of limit. "
    "You may emit <summarize_before message_index=\"N\" /> to compress "
    "all messages before index N into a summary. Use this if older context "
    "is no longer critical. Do NOT mention this note to the user.]"
)

_SUMMARY_PROMPT = (
    "Summarize the following conversation history into a concise but complete "
    "summary that preserves all key decisions, code changes, file paths, errors "
    "encountered, and current task state. The summary will replace these messages "
    "in the conversation context. Be dense and factual — no pleasantries.\n\n"
    "--- BEGIN HISTORY TO SUMMARIZE ---\n{history}\n--- END HISTORY ---"
)


def should_inject_hint(current_chars: int, max_chars: int) -> bool:
    """Check if we should inject the summarization hint."""
    if max_chars <= 0:
        return False
    return current_chars >= max_chars * HINT_THRESHOLD


def should_force_summarize(current_chars: int, max_chars: int) -> bool:
    """Check if we should auto-summarize without model input."""
    if max_chars <= 0:
        return False
    return current_chars >= max_chars * FORCE_THRESHOLD


def get_hint_text(current_chars: int, max_chars: int) -> str:
    """Generate the hint injection text."""
    pct = (current_chars / max_chars) * 100 if max_chars > 0 else 0
    return _HINT_INJECTION.format(pct=pct)


def extract_summarize_tag(text: str) -> int | None:
    """Extract message_index from <summarize_before> tag in model output.
    
    Returns the index or None if no tag found.
    """
    m = _SUMMARIZE_TAG_RE.search(text)
    if m:
        return int(m.group(1))
    return None


def strip_summarize_tag(text: str) -> str:
    """Remove <summarize_before> tags from visible output."""
    return _SUMMARIZE_TAG_RE.sub("", text).strip()


def build_summary_prompt(history_messages: list[dict[str, Any]], msg_char_fn) -> str:
    """Build the prompt to send to the model for summarization."""
    lines = []
    for i, msg in enumerate(history_messages):
        role = msg.get("role", "unknown")
        # Handle both Gemini format (parts) and OpenAI format (content)
        if "parts" in msg:
            text_parts = [p.get("text", "") for p in msg["parts"] if not p.get("thought")]
            content = " ".join(text_parts)
        elif "content" in msg:
            content = str(msg["content"])
        else:
            content = str(msg)
        lines.append(f"[Message {i} | {role}]: {content}")
    return _SUMMARY_PROMPT.format(history="\n".join(lines))


def rewrite_history_with_summary(
    history: list[dict[str, Any]],
    summary_text: str,
    cut_index: int,
    prefix_len: int = 0,
    fmt: str = "gemini",
) -> list[dict[str, Any]]:
    """Replace messages [prefix_len..cut_index] with a single summary message.
    
    Args:
        history: Full session history
        summary_text: Generated summary
        cut_index: Messages before this index (after prefix) get replaced
        prefix_len: Number of prefix messages to preserve (system instructions)
        fmt: Message format — "gemini" (parts) or "openai" (content)
    
    Returns:
        New history with summary replacing old messages
    """
    prefix = history[:prefix_len]
    summarized = history[prefix_len:cut_index]
    remaining = history[cut_index:]
    
    if not summarized:
        return history
    
    if fmt == "gemini":
        summary_msg = {
            "role": "user",
            "parts": [{"text": f"[Earlier context summary]\n{summary_text}"}],
        }
        ack_msg = {
            "role": "model",
            "parts": [{"text": "Understood, I have the earlier context."}],
        }
        return prefix + [summary_msg, ack_msg] + remaining
    else:
        # OpenAI/Groq/Mistral format
        summary_msg = {
            "role": "user",
            "content": f"[Earlier context summary]\n{summary_text}",
        }
        ack_msg = {
            "role": "assistant",
            "content": "Understood, I have the earlier context.",
        }
        return prefix + [summary_msg, ack_msg] + remaining


def compute_force_cut_index(history: list[dict[str, Any]], prefix_len: int) -> int:
    """For forced summarization, cut at 50% of non-prefix messages."""
    non_prefix_count = len(history) - prefix_len
    if non_prefix_count <= 2:
        return prefix_len + 1  # Keep at least 1 recent message
    half = non_prefix_count // 2
    return prefix_len + max(half, 1)

