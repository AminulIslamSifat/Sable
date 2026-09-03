

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
import os

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Coroutine


logger = logging.getLogger(__name__)

# Signal function: async (chat_id, message_text) -> None
SignalFn = Callable[[str, str], Coroutine[Any, Any, None]]


@dataclass
class _TeacherRequest:
    """A pending teacher escalation request from a subagent."""
    agent_id: str
    role: str
    task: str
    stuck_reason: str
    todo_snapshot: list[dict] | None
    future: asyncio.Future = field(default=None)  # type: ignore[assignment]
    created_at: float = field(default_factory=time.time)


@dataclass
class _ChatState:
    """Per-chat auto-turn state. Ephemeral."""

    chat_id: str
    busy: bool = False
    queue: list[dict[str, str]] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    created_at: float = field(default_factory=time.time)
    # Agent IDs spawned during the current active stream — results delivered via skill cards
    current_stream_agents: set[str] = field(default_factory=set)
    # Conversation settings (set by chat route on each message)
    model: str | None = None
    thinking_mode: str | None = None
    provider: str | None = None  # "qwen" | "deepseek" | "scraping"
    # Pending teacher escalation requests waiting for main chat response
    pending_teacher_requests: dict[str, _TeacherRequest] = field(default_factory=dict)


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
        state.current_stream_agents.clear()

    def register_stream_agent(self, chat_id: str, agent_id: str) -> None:
        """Track an agent spawned during the current stream (result via skill card)."""
        state = self.ensure_chat(chat_id)
        state.current_stream_agents.add(agent_id)

    def mark_stream_done(self, chat_id: str) -> None:
        """Called when the main chat stream ends — drains queued agent results."""
        state = self._chats.get(chat_id)
        if not state:
            return
        state.busy = False
        state.current_stream_agents.clear()
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
        from engine.config import AGENT_OUTPUT_DIR as _aod
        _output_dir = str(_aod)
        summary = (
            f"[Agent {agent_id} ({role}) SUCCEEDED]\n"
            f"Task: {task_snippet}\n"
            f"Result saved to: {os.path.join(_output_dir, agent_id + '.md')}"
            f"Full log (step by step progress, tool call, etc) + result saved to: {os.path.join(_output_dir, agent_id + '_conversation.md')}"
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
        from engine.config import AGENT_OUTPUT_DIR as _aod
        _output_dir = str(_aod)
        summary = (
            f"[Agent {agent_id} ({role}) FAILED]\n"
            f"Task: {task_snippet}\n"
            f"Error: {error}\n"
            f"Full log saved to: {_output_dir}/{agent_id}.md"
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
            if state.busy:
                # Another turn started while we were preparing — queue instead
                for line in result_lines:
                    state.queue.append({"role": "user", "content": line})
                return
            state.busy = True

        prompt = (
            "The following agent(s) have completed their tasks. "
            "Acknowledge the result and take any necessary follow-up action. "
            "You may use tool calls wrapped in <tool_call>...</tool_call> blocks if needed.\n\n"
            + "\n\n".join(result_lines)
        )

        logger.info("[auto_turn] signalling frontend for chat %s", chat_id)

        try:
            await self._signal_fn(chat_id, prompt)
        except Exception as exc:
            logger.error("[auto_turn] signal failed for chat %s: %s", chat_id, exc)
            state.busy = False
            # Don't drain on failure — let next agent event or stream-done handle it


    # ------------------------------------------------------------------
    # Teacher escalation: subagent asks main chat for guidance
    # ------------------------------------------------------------------

    async def request_teacher_guidance(
        self, chat_id: str, agent_id: str, role: str, task: str,
        stuck_reason: str, todo_snapshot: list[dict] | None = None,
        context: str | None = None, recent_messages: list[dict] | None = None,
    ) -> str | None:
        """Request teacher guidance from main chat. Blocks until main chat responds.

        Sends the escalation as a queued message (like agent completion) so it
        gets delivered to main chat on the next available turn. Main chat responds
        by calling the teacher_guidance tool, which resolves the future.

        Returns guidance text or None on timeout.
        """
        state = self.ensure_chat(chat_id)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[str, list[dict] | None]] = loop.create_future()

        request = _TeacherRequest(
            agent_id=agent_id,
            role=role,
            task=task,
            stuck_reason=stuck_reason,
            todo_snapshot=todo_snapshot,
            future=future,
        )
        state.pending_teacher_requests[agent_id] = request

        # Build the prompt for main chat — includes tool schema so it knows how to respond
        todo_section = ""
        if todo_snapshot:
            todo_lines = []
            for t in todo_snapshot:
                icon = {"completed": "✅", "in_progress": "🔧", "pending": "❌", "skipped": "⏭️"}.get(t.get("status", ""), "?")
                todo_lines.append(f"  {icon} [{t.get('id')}] {t.get('content')} — {t.get('status')}")
                if t.get("result"):
                    todo_lines.append(f"       Result: {t['result']}")
            todo_section = "\nTodo List:\n" + "\n".join(todo_lines)

        context_section = f"\nContext: {context}" if context else ""

        recent_section = ""
        if recent_messages:
            msg_lines = [f"[{m.get('role', '?')}]: {m.get('content', '')[:400]}" for m in recent_messages[-6:]]
            recent_section = "\nRecent conversation:\n" + "\n".join(msg_lines)

        escalation_prompt = (
            f"[TEACHER ESCALATION REQUEST]\n"
            f"Agent {agent_id} ({role}) is stuck and needs your guidance.\n\n"
            f"Task: {task}\n"
            f"Stuck reason: {stuck_reason}\n"
            f"{context_section}{todo_section}{recent_section}\n\n"
            f"Please analyze what the agent is doing wrong and provide guidance.\n"
            f"Respond using the teacher_guidance tool:\n\n"
            f'<tool_call>\n'
            f'{{"name": "teacher_guidance", "arguments": {{\n'
            f'  "agent_id": "{agent_id}",\n'
            f'  "guidance": "your specific actionable guidance here",\n'
            f'  "todo_updates": "[{{\"action\": \"add\", \"content\": \"new step\"}}]"\n'
            f'}}}}\n'
            f'</tool_call>\n\n'
            f"The todo_updates field is optional JSON. Supported actions: add, remove, replace, skip.\n"
            f"Only include todo_updates if the plan itself is flawed."
        )

        # Queue it like agent completion — gets delivered on next available turn
        async with state.lock:
            if state.busy:
                state.queue.append({"role": "user", "content": escalation_prompt})
                logger.info("[auto_turn] queued teacher escalation for agent %s (busy)", agent_id)
            else:
                # Main chat is idle — fire immediately
                asyncio.create_task(self._fire_turn(chat_id, [escalation_prompt]))
                logger.info("[auto_turn] fired teacher escalation for agent %s (idle)", agent_id)

        # Wait for main chat to respond via teacher_guidance tool call
        try:
            guidance_text, todo_updates = await asyncio.wait_for(future, timeout=300)  # 5 min timeout
            logger.info("[auto_turn] teacher guidance received for agent %s", agent_id)
            return guidance_text
        except asyncio.TimeoutError:
            logger.warning("[auto_turn] teacher guidance timed out for agent %s", agent_id)
            return None
        finally:
            state.pending_teacher_requests.pop(agent_id, None)

    def get_pending_teacher_prompts(self, chat_id: str) -> list[str]:
        """Return pending teacher escalation prompts for injection into tool feedback.

        Called during active stream to flush pending teacher requests alongside
        tool results, so Maria can respond mid-stream instead of waiting for
        stream completion. Returns the escalation prompt strings and removes
        them from the queue (they've been delivered).
        """
        state = self._chats.get(chat_id)
        if not state or not state.pending_teacher_requests:
            return []

        prompts: list[str] = []
        # Only flush requests that were queued while busy (not ones already fired)
        # Check the queue for teacher escalation messages
        remaining_queue: list[dict[str, str]] = []
        for item in state.queue:
            content = item.get("content", "")
            if content.startswith("[TEACHER ESCALATION REQUEST]"):
                prompts.append(content)
            else:
                remaining_queue.append(item)
        state.queue = remaining_queue

        return prompts

    def resolve_teacher_guidance(
        self, agent_id: str, guidance: str, todo_updates: list[dict] | None = None,
    ) -> None:
        """Called by teacher_guidance handler when main chat responds.

        Resolves the future so the waiting subagent unblocks.
        Also applies todo_updates to the agent if provided.
        """
        # Find which chat has this pending request
        for state in self._chats.values():
            req = state.pending_teacher_requests.get(agent_id)
            if req and req.future and not req.future.done():
                # Apply todo updates to the agent before resolving
                if todo_updates:
                    try:
                        from engine.agents import get_runtime
                        runtime = get_runtime()
                        agent = runtime._agents.get(agent_id)
                        if agent and agent.todos:
                            from engine.agents.teacher import _apply_todo_updates
                            _apply_todo_updates(agent, todo_updates)
                            logger.info("[auto_turn] applied %d todo updates for agent %s",
                                        len(todo_updates), agent_id)
                    except Exception as exc:
                        logger.warning("[auto_turn] todo update failed for agent %s: %s", agent_id, exc)

                req.future.set_result((guidance, todo_updates))
                logger.info("[auto_turn] resolved teacher guidance for agent %s", agent_id)
                return

        logger.warning("[auto_turn] no pending teacher request for agent %s", agent_id)


# Singleton
auto_turn = AutoTurnEngine()
