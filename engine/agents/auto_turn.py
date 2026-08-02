

"""Auto-turn engine: signals the frontend to run a normal chat turn with agent results.

Lifecycle:
- Per-chat state is created on first agent spawn, destroyed when idle + queue empty.
- The on_agent_done callback is registered once on the runtime (permanent, zero-cost).
- If the model is idle -> immediately signal the frontend with agent results.
- If the model is busy -> queue results; the active stream's finally block calls drain().

The frontend receives an ``auto_turn_trigger`` event via the agent-events SSE and
initiates a normal ``POST /api/chat`` call, so the response renders identically to
a user-initiated message (full skill cards, stop button, markdown, history replay).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

# Signal function: async (chat_id, message_text) -> None
SignalFn = Callable[[str, str], Coroutine[Any, Any, None]]


@dataclass
class _ChatState:
    """Per-chat auto-turn state. Ephemeral."""

    chat_id: str
    busy: bool = False
    queue: list[dict[str, str]] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    created_at: float = field(default_factory=time.time)
    # Conversation settings (set by chat route on each message)
    model: str | None = None
    thinking_mode: str | None = None
    provider: str | None = None  # "qwen" | "deepseek" | "scraping"


class AutoTurnEngine:
    """Manages autonomous model turns triggered by agent completions."""

    def __init__(self) -> None:
        self._chats: dict[str, _ChatState] = {}
        self._signal_fn: SignalFn | None = None

    def set_signal_fn(self, fn: SignalFn) -> None:
        """Set the function that signals the frontend to start a normal chat turn."""
        self._signal_fn = fn

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def set_chat_settings(self, chat_id: str, model: str | None, thinking_mode: str | None, provider: str | None) -> None:
        """Store conversation settings so auto-turn uses the same model/provider."""
        state = self.ensure_chat(chat_id)
        state.model = model
        state.thinking_mode = thinking_mode
        state.provider = provider

    def get_chat_settings(self, chat_id: str) -> tuple[str | None, str | None, str | None]:
        """Returns (model, thinking_mode, provider) for a chat."""
        state = self._chats.get(chat_id)
        if state:
            return state.model, state.thinking_mode, state.provider
        return None, None, None

    def ensure_chat(self, chat_id: str) -> _ChatState:
        """Get or create per-chat state. Call on agent spawn."""
        if chat_id not in self._chats:
            self._chats[chat_id] = _ChatState(chat_id=chat_id)
            logger.debug("[auto_turn] created state for chat %s", chat_id)
        return self._chats[chat_id]

    def cleanup_chat(self, chat_id: str) -> None:
        """Remove per-chat state if idle and queue is empty."""
        state = self._chats.get(chat_id)
        if state and not state.busy and not state.queue:
            del self._chats[chat_id]
            logger.debug("[auto_turn] cleaned up chat %s", chat_id)

    def mark_stream_busy(self, chat_id: str) -> None:
        """Called when the main chat stream starts — prevents auto-turn firing."""
        state = self.ensure_chat(chat_id)
        state.busy = True

    def mark_stream_done(self, chat_id: str) -> None:
        """Called when the main chat stream ends — drains queued agent results."""
        state = self._chats.get(chat_id)
        if not state:
            return
        state.busy = False
        # Schedule drain on the event loop (this may be called from sync context)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.drain(chat_id))
        except RuntimeError:
            pass  # No running loop — drain will happen on next agent event

    # ------------------------------------------------------------------
    # Core: agent done -> signal frontend
    # ------------------------------------------------------------------

    async def on_agent_done(self, chat_id: str, agent_id: str, role: str, result: str, task: str = "") -> None:
        """Called by runtime when an agent completes. Fires or queues a signal."""
        state = self.ensure_chat(chat_id)
        task_snippet = (task[:80] + "…") if len(task) > 80 else task
        summary = (
            f"[Agent {agent_id} ({role}) SUCCEEDED]\n"
            f"Task: {task_snippet}\n"
            f"Full log + result saved to: output/agent/{agent_id}.md"
        )

        async with state.lock:
            if state.busy:
                state.queue.append({"role": "user", "content": summary})
                logger.info("[auto_turn] queued result for chat %s (busy)", chat_id)
                return

        await self._fire_turn(chat_id, [summary])

    async def on_agent_failed(self, chat_id: str, agent_id: str, role: str, error: str, task: str = "") -> None:
        """Called by runtime when an agent fails."""
        state = self.ensure_chat(chat_id)
        task_snippet = (task[:80] + "…") if len(task) > 80 else task
        summary = (
            f"[Agent {agent_id} ({role}) FAILED]\n"
            f"Task: {task_snippet}\n"
            f"Error: {error}\n"
            f"Full log saved to: output/agent/{agent_id}.md"
        )

        async with state.lock:
            if state.busy:
                state.queue.append({"role": "user", "content": summary})
                return

        await self._fire_turn(chat_id, [summary])

    # ------------------------------------------------------------------
    # Drain (called from active stream's finally block)
    # ------------------------------------------------------------------

    async def drain(self, chat_id: str) -> None:
        """Process queued agent results. Call when a stream/turn ends."""
        state = self._chats.get(chat_id)
        if not state:
            return

        async with state.lock:
            if not state.queue:
                state.busy = False
                self.cleanup_chat(chat_id)
                return
            pending = [m["content"] for m in state.queue]
            state.queue.clear()

        await self._fire_turn(chat_id, pending)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _fire_turn(self, chat_id: str, result_lines: list[str]) -> None:
        """Signal the frontend to run a normal chat turn with agent results."""
        if not self._signal_fn:
            logger.warning("[auto_turn] no signal_fn set, dropping results for %s", chat_id)
            return

        state = self.ensure_chat(chat_id)
        async with state.lock:
            state.busy = True

        prompt = (
            "The following agent(s) have completed their tasks. "
            "Acknowledge the result and take any necessary follow-up action. "
            "You may use tool tags wrapped in <action> blocks if needed.\n\n"
            + "\n\n".join(result_lines)
        )

        logger.info("[auto_turn] signalling frontend for chat %s", chat_id)

        try:
            await self._signal_fn(chat_id, prompt)
        except Exception as exc:
            logger.error("[auto_turn] signal failed for chat %s: %s", chat_id, exc)
        finally:
            state.busy = False
            await self.drain(chat_id)


# Singleton
auto_turn = AutoTurnEngine()
