
"""Auto-turn engine: feeds agent results back to the model autonomously.

Lifecycle:
- Per-chat state is created on first agent spawn, destroyed when idle + queue empty.
- The on_agent_done callback is registered once on the runtime (permanent, zero-cost).
- If the model is idle -> immediately fire a synthetic turn with agent results.
- If the model is busy -> queue results; the active stream's finally block calls drain().
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

# Type for the LLM turn function: async (chat_id, messages, on_chunk) -> full_response
TurnFn = Callable[[str, list[dict[str, str]], Callable[[str], Coroutine]], Coroutine[Any, Any, str]]


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
        self._turn_fn: TurnFn | None = None
        self._on_chunk: Callable[[str, str], Coroutine] | None = None

    def set_turn_fn(self, fn: TurnFn) -> None:
        """Set the function that runs an LLM completion turn."""
        self._turn_fn = fn

    def set_chunk_callback(self, fn: Callable[[str, str], Coroutine]) -> None:
        """Set callback for streaming tokens to frontend. (chat_id, token)."""
        self._on_chunk = fn

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
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.drain(chat_id))
        except RuntimeError:
            pass  # No running loop — drain will happen on next agent event

    # ------------------------------------------------------------------
    # Core: agent done -> auto turn
    # ------------------------------------------------------------------

    async def on_agent_done(self, chat_id: str, agent_id: str, role: str, result: str, task: str = "") -> None:
        """Called by runtime when an agent completes. Fires or queues a turn."""
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
        """Run a synthetic model turn with agent results as context."""
        if not self._turn_fn:
            logger.warning("[auto_turn] no turn_fn set, dropping results for %s", chat_id)
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
        messages = [{"role": "user", "content": prompt}]

        turn_id = str(uuid.uuid4())[:8]
        logger.info("[auto_turn] firing turn %s for chat %s", turn_id, chat_id)

        async def _chunk_cb(token: str) -> None:
            if self._on_chunk:
                await self._on_chunk(chat_id, token)

        try:
            await self._turn_fn(chat_id, messages, _chunk_cb)
        except Exception as exc:
            logger.error("[auto_turn] turn %s failed: %s", turn_id, exc)
        finally:
            await self.drain(chat_id)


# Singleton
auto_turn = AutoTurnEngine()
