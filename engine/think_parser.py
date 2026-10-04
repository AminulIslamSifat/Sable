"""Universal <think>…</think> splitter for connector output.

Some providers (local llama-server without a reasoning_content field, MiMo,
raw OpenAI-compat endpoints, Groq, custom Cloudflare endpoints, etc.) embed
chain-of-thought inside the *content* stream wrapped in ``<think>`` /
``</think>`` (or ``<thinking>`` variants) instead of exposing a native
``reasoning_content`` delta. When that happens the raw tags leak into the
visible answer.

Every connector *should* separate reasoning, but new/edge providers slip
through. This module is the single, provider-agnostic safety net: any answer
text routed through :class:`ThinkTagSplitter` is split into
``answer`` / ``thinking`` segments, honoring chunk boundaries (a tag split
across two SSE chunks is reassembled, never leaked).

Design goals:

* **Streaming-safe** — keeps a small look-behind buffer for partial tags.
* **Idempotent** — if a connector already stripped its tags, this is a no-op.
* **Zero config** — works for every provider without per-backend wiring.
"""

from __future__ import annotations

import re
from typing import Iterator

# Opening / closing tag names we recognise.
_OPEN_TAGS = ("<think>", "<thinking>", "<redacted_thinking>")
_CLOSE_TAGS = ("</think>", "</thinking>", "</redacted_thinking>")
_ALL_TAGS = _OPEN_TAGS + _CLOSE_TAGS

_MAX_TAG_LEN = max(len(t) for t in _ALL_TAGS)

# Matches a complete think block (used by the non-streaming helper).
_COMPLETE_RE = re.compile(
    r"<(think|thinking|redacted_thinking)>(.*?)</\1>",
    re.DOTALL | re.IGNORECASE,
)

_ORPHAN_CLOSE_RE = re.compile(
    r"</(?:think|thinking|redacted_thinking)>", re.IGNORECASE
)


class ThinkTagSplitter:
    """Stateful, streaming-safe ``<think>`` tag splitter.

    Feed it raw answer chunks in order; it yields ``(kind, text)`` tuples where
    ``kind`` is ``"answer"`` or ``"thinking"``. Call :meth:`flush` once at the
    end of the stream to drain any held-back partial tag.
    """

    __slots__ = ("_buf", "_in_think", "_tag")

    def __init__(self) -> None:
        self._buf = ""            # carry buffer for partial tags
        self._in_think = False    # currently inside a think block
        self._tag = ""            # partial tag being assembled

    def _is_partial_tag(self, text: str) -> bool:
        """True if *text* is a proper prefix of any recognised tag."""
        return any(t.startswith(text) for t in _ALL_TAGS)

    def _emit(self, kind: str, text: str) -> Iterator[tuple[str, str]]:
        if text:
            yield (kind, text)

    def feed(self, chunk: str) -> Iterator[tuple[str, str]]:
        """Process a raw answer chunk, yielding ``(kind, text)`` tuples."""
        if not chunk:
            return

        data = self._buf + chunk
        self._buf = ""

        # Fast path: no tag chars anywhere and not inside think mode.
        if "<" not in data and not self._in_think and not self._tag:
            yield from self._emit("answer", data)
            return

        i = 0
        n = len(data)
        answer: list[str] = []
        thinking: list[str] = []

        while i < n:
            ch = data[i]

            # We are assembling a candidate tag.
            if self._tag:
                candidate = self._tag + ch
                if self._is_partial_tag(candidate):
                    self._tag = candidate
                    if candidate in _ALL_TAGS:
                        self._in_think = candidate in _OPEN_TAGS
                        self._tag = ""
                    i += 1
                    continue
                # Candidate is not a tag prefix. Flush only the buffered
                # text (which excludes ch), then handle ch on its own. This
                # avoids emitting ch twice when it can seed a new candidate.
                literal = self._tag
                self._tag = ""
                (thinking if self._in_think else answer).append(literal)
                if ch == "<":
                    self._tag = "<"
                else:
                    (thinking if self._in_think else answer).append(ch)
                i += 1
                continue

            if ch == "<":
                self._tag = "<"
                i += 1
                continue

            if self._in_think:
                thinking.append(ch)
            else:
                answer.append(ch)
            i += 1

        if thinking:
            yield from self._emit("thinking", "".join(thinking))
        if answer:
            yield from self._emit("answer", "".join(answer))

    def flush(self) -> Iterator[tuple[str, str]]:
        """Drain any held-back partial tag / buffer at end of stream."""
        leftover = self._tag
        self._tag = ""
        if leftover:
            yield from self._emit("thinking" if self._in_think else "answer", leftover)


# ---------------------------------------------------------------------------
# One-shot helpers (non-streaming paths)
# ---------------------------------------------------------------------------

def split_thinking(text: str) -> tuple[str, str]:
    """Split a complete string into ``(answer, thinking)``."""
    if not text or "<" not in text:
        return text or "", ""

    thinking_parts: list[str] = []

    def _repl(m: re.Match[str]) -> str:
        thinking_parts.append(m.group(2))
        return ""

    answer = _COMPLETE_RE.sub(_repl, text)
    answer = _ORPHAN_CLOSE_RE.sub("", answer)
    return answer, "".join(thinking_parts).strip()


def strip_thinking(text: str) -> str:
    """Return *text* with all ``<think>`` blocks removed (answer only)."""
    return split_thinking(text)[0].strip()
