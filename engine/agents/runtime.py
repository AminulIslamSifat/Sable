
"""AgentRuntime — spawns, tracks, and manages background agent tasks."""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import time
from pathlib import Path
from typing import Any, Callable, Coroutine

from engine.agents.agent import Agent
from engine.agents.notifications import notification_queue
from engine.agents.protocol import AgentEvent, AgentResult, AgentStatus, TaskAssignment
from engine.agents.registry import get_role_config, get_next_account
from engine.agents.auto_turn import auto_turn
from engine.agents.resilience import CircuitBreaker

logger = logging.getLogger("sable")

# Agent output directory — use central config so path stays in sync
from engine.config import AGENT_OUTPUT_DIR as _AGENT_OUTPUT_DIR

# Type for the SSE push callback: async fn(chat_id, event_dict)
EventCallback = Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]]


class AgentRuntime:
    """Manages agent lifecycle: spawn → run → complete/fail.

    Concurrency controlled via semaphores (per-backend + global).
    Circuit breakers prevent hammering dead providers.
    """

    # Agents older than this are pruned on next spawn/completion cycle
    _PRUNE_AFTER_SECONDS: float = 3600.0  # 1 hour

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        conc = cfg.get("concurrency", {})
        res = cfg.get("resilience", {})

        self._agents: dict[str, Agent] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._spawn_lock = asyncio.Lock()
        self._max_agents: int = conc.get("global_max", 5)
        self._max_depth: int = 3

        # Per-backend semaphores
        self._ds_sem = asyncio.Semaphore(conc.get("deepseek_max", 5))
        self._qwen_sem = asyncio.Semaphore(conc.get("qwen_max", 1))
        self._global_sem = asyncio.Semaphore(self._max_agents)

        # Circuit breakers
        threshold = res.get("circuit_breaker_threshold", 5)
        reset = res.get("circuit_breaker_reset_seconds", 60)
        self._breakers: dict[str, CircuitBreaker] = {
            "deepseek": CircuitBreaker(threshold, reset),
            "qwen": CircuitBreaker(threshold, reset),
            "gemini": CircuitBreaker(threshold, reset),
            "groq": CircuitBreaker(threshold, reset),
            "mistral": CircuitBreaker(threshold, reset),
        }

        # Loop limits (max iterations, tool call caps)
        limits = cfg.get("limits", {})
        self._limits: dict[str, int] = {
            "max_iterations": limits.get("max_iterations", 25),
            "max_consecutive_tool_calls": limits.get("max_consecutive_tool_calls", 15),
            "max_total_tool_calls": limits.get("max_total_tool_calls", 50),
        }

        # SSE event callback (set by API layer)
        self._event_callback: EventCallback | None = None

        # Main event loop reference (set from the async layer before any thread-pool dispatch)
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def set_event_callback(self, callback: EventCallback) -> None:
        """Set the async callback that pushes events to SSE clients."""
        self._event_callback = callback

    def update_config(self, config: dict[str, Any]) -> None:
        """Hot-reload concurrency/resilience/limits settings."""
        conc = config.get("concurrency", {})
        res = config.get("resilience", {})
        limits = config.get("limits", {})
        self._max_agents = conc.get("global_max", self._max_agents)
        # Recreate semaphores with new limits (safe — asyncio.Semaphore has no running-state leak)
        if "deepseek_max" in conc:
            self._ds_sem = asyncio.Semaphore(conc["deepseek_max"])
        if "qwen_max" in conc:
            self._qwen_sem = asyncio.Semaphore(conc["qwen_max"])
        if "global_max" in conc:
            self._global_sem = asyncio.Semaphore(conc["global_max"])
        # Update breaker thresholds
        for breaker in self._breakers.values():
            breaker.threshold = res.get("circuit_breaker_threshold", breaker.threshold)
            breaker.reset_timeout = res.get("circuit_breaker_reset_seconds", breaker.reset_timeout)
        # Update loop limits
        if limits:
            self._limits["max_iterations"] = limits.get("max_iterations", self._limits["max_iterations"])
            self._limits["max_consecutive_tool_calls"] = limits.get("max_consecutive_tool_calls", self._limits["max_consecutive_tool_calls"])
            self._limits["max_total_tool_calls"] = limits.get("max_total_tool_calls", self._limits["max_total_tool_calls"])

    # ------------------------------------------------------------------
    # Spawning
    # ------------------------------------------------------------------

    @property
    def active_count(self) -> int:
        return sum(1 for a in self._agents.values() if a.status == AgentStatus.RUNNING)

    def _prune_stale_agents(self) -> None:
        """Remove completed/failed agents older than _PRUNE_AFTER_SECONDS."""
        cutoff = time.time() - self._PRUNE_AFTER_SECONDS
        stale_ids = [
            aid for aid, a in self._agents.items()
            if a.status != AgentStatus.RUNNING
            and a.completed_at is not None
            and a.completed_at < cutoff
        ]
        for aid in stale_ids:
            del self._agents[aid]
            self._tasks.pop(aid, None)
        if stale_ids:
            logger.debug("Pruned %d stale agents", len(stale_ids))

    async def spawn(self, assignment: TaskAssignment, chat_id: str) -> Agent:
        """Spawn a new agent. Non-blocking — returns immediately."""
        async with self._spawn_lock:
            # Prune old agents to free slots
            self._prune_stale_agents()

            if self.active_count >= self._max_agents:
                raise RuntimeError(f"Max agents ({self._max_agents}) reached")

            # Depth check
            depth = 0
            if assignment.parent_agent_id:
                parent = self._agents.get(assignment.parent_agent_id)
                depth = (parent.depth + 1) if parent else 1
            if depth >= self._max_depth:
                raise RuntimeError(f"Max depth ({self._max_depth}) reached")

            # Resolve config: tag attrs > role_overrides > defaults
            role_cfg = get_role_config(assignment.role)
            # Resolve browser account: explicit > pool (skip in-use) > None
            browser_dir = assignment.browser_data_dir
            if not browser_dir:
                in_use = {a.browser_data_dir for a in self._agents.values() if a.browser_data_dir and a.status == AgentStatus.RUNNING}
                assigned_account = get_next_account(assignment.role, in_use)
                if assigned_account:
                    from engine.config import _SYSTEM as _AGENT_SYSTEM_DIR
                    acct_profile = _AGENT_SYSTEM_DIR / assigned_account
                    if acct_profile.is_dir():
                        browser_dir = str(acct_profile)
                    else:
                        browser_dir = assigned_account  # fallback to raw name if dir missing

            agent = Agent(
                role=assignment.role,
                task=assignment.task,
                context=assignment.context,
                instruction=assignment.instruction,
                model=assignment.model or role_cfg.default_model,
                browser_data_dir=browser_dir,
                parent_id=assignment.parent_agent_id,
                chat_id=chat_id,
                depth=depth,
                collect=assignment.collect,
            )

            # Attach todo list if provided
            if assignment.todos:
                from engine.agents.agent import AgentTodoList
                agent.todos = AgentTodoList.build_from_list(assignment.todos)

            # Attach fallback chain from role config
            agent.model_chain = role_cfg.model_chain

            self._agents[agent.id] = agent

        # DB persist
        from server.database import insert_agent_run
        insert_agent_run(
            agent_id=agent.id,
            chat_id=chat_id,
            role=agent.role,
            task=agent.task,
            path=agent.path,
            depth=depth,
            parent_agent_id=agent.parent_id,
            model=agent.model,
            browser_data_dir=agent.browser_data_dir,
        )

        # Clear Qwen account settings (disable built-in tools + empty instruction)
        # on EVERY spawn for Qwen agents with a browser profile.
        if agent.browser_data_dir and "qwen" in agent.model:
            try:
                from engine.agents.loop import _clear_qwen_account_settings, _get_agent_qwen_headers
                spawn_headers = await _get_agent_qwen_headers(agent)
                await _clear_qwen_account_settings(spawn_headers, agent.id)
            except Exception as exc:
                logger.warning("Agent %s: clear settings on spawn failed: %s", agent.id, exc)

        # Emit spawn event
        await self._emit(chat_id, AgentEvent(
            type="agent_spawned",
            agent_id=agent.id,
            data={
                "role": agent.role,
                "task": agent.task,
                "model": agent.model,
                "todos": [
                    {"id": t.id, "content": t.content, "status": t.status, "subtasks": t.subtasks, "result": t.result}
                    for t in agent.todos.todos
                ] if agent.todos else None,
            },
        ))

        # Select semaphore based on backend
        sem = self._qwen_sem if "qwen" in agent.model else self._ds_sem
        timeout = assignment.timeout or role_cfg.default_timeout

        # Fire-and-forget
        task = asyncio.create_task(self._run_agent(agent, sem, timeout))
        self._tasks[agent.id] = task
        return agent

    # ------------------------------------------------------------------
    # Agent execution
    # ------------------------------------------------------------------

    async def _run_agent(self, agent: Agent, sem: asyncio.Semaphore, timeout: float) -> None:
        """Run agent within semaphore bounds. Handles all failure modes."""
        async with self._global_sem, sem:
            agent.mark_running()
            from server.database import update_agent_status
            update_agent_status(agent.id, "running")

            try:
                from engine.agents.loop import run_agent_llm_loop
                result = await asyncio.wait_for(
                    run_agent_llm_loop(agent, self._breakers, self._limits),
                    timeout=timeout,
                )
                agent.mark_completed(result)
                # Signal panel stream that the agent is done
                agent.push_stream_event({"type": "done", "result": (result or "")[:500]})
                update_agent_status(
                    agent.id, agent.status.value,
                    result=result, tokens_used=agent.tokens_used,
                )
                # Agent produces markdown natively — result is already a string
                final_result = result or "No result"
                await self._emit(agent.chat_id, AgentEvent(
                    type="agent_completed",
                    agent_id=agent.id,
                    data={
                        "role": agent.role,
                        "result": final_result,
                        "words": agent.word_count,
                        "duration": agent.duration,
                        "skills_used": agent.skills_used,
                        "model": agent.model,
                        "browser_data_dir": agent.browser_data_dir or "",
                    },
                ))
                # Auto-turn: feed brief notification back to model
                if agent.chat_id:
                    await auto_turn.on_agent_done(agent.chat_id, agent.id, agent.role, result, task=agent.task)

                # Memory trigger: consolidate agent knowledge if thresholds exceeded
                try:
                    from engine.agents.memory_trigger import trigger_agent_memory
                    await trigger_agent_memory(agent)
                except Exception as exc:
                    logger.debug("[memory_trigger] Failed for agent %s: %s", agent.id, exc)

            except asyncio.TimeoutError:
                partial = agent.messages[-1]["content"] if agent.messages else ""
                agent.mark_failed(f"Timed out after {timeout}s")
                agent.push_stream_event({"type": "error", "message": f"Timed out after {timeout}s"})
                agent.result = partial
                update_agent_status(agent.id, "timed_out", error=agent.error, result=partial)
                # Persist failure reason into agent conversation history
                fail_msg = f"[SYSTEM] Agent failed: {agent.error}"
                agent.messages.append({"role": "system", "content": fail_msg})
                await self._persist_failure(agent.id, fail_msg)
                await self._emit(agent.chat_id, AgentEvent(
                    type="agent_failed",
                    agent_id=agent.id,
                    data={"role": agent.role, "error": agent.error, "partial": partial[:300]},
                ))
                if agent.chat_id:
                    await auto_turn.on_agent_failed(agent.chat_id, agent.id, agent.role, agent.error, task=agent.task)

            except Exception as exc:
                agent.mark_failed(f"{type(exc).__name__}: {exc}")
                agent.push_stream_event({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
                update_agent_status(agent.id, "failed", error=agent.error)
                # Persist failure reason into agent conversation history
                fail_msg = f"[SYSTEM] Agent failed: {agent.error}"
                agent.messages.append({"role": "system", "content": fail_msg})
                await self._persist_failure(agent.id, fail_msg)
                await self._emit(agent.chat_id, AgentEvent(
                    type="agent_failed",
                    agent_id=agent.id,
                    data={"role": agent.role, "error": agent.error},
                ))
                if agent.chat_id:
                    await auto_turn.on_agent_failed(agent.chat_id, agent.id, agent.role, agent.error, task=agent.task)

            # Single-path notification: only queue if auto_turn won't handle it
            # Collect-mode agents are handled inline by chat.py — skip both paths
            if agent.chat_id and not agent.collect:
                event_type = "agent_completed" if agent.status in (AgentStatus.COMPLETED, AgentStatus.DEGRADED) else "agent_failed"
                _evt_data = {
                    "role": agent.role,
                    "summary": (agent.result or "")[:500],
                    "error": agent.error,
                    "duration": agent.duration,
                    "words": agent.word_count,
                    "skills_used": agent.skills_used,
                    "model": agent.model,
                    "browser_data_dir": agent.browser_data_dir or "",
                }
                # Check if auto_turn considers the chat busy (model mid-stream)
                from engine.agents.auto_turn import auto_turn as _at
                _at_state = _at._chats.get(agent.chat_id)
                _is_busy = _at_state and _at_state.busy
                # Check if this agent was spawned during the current stream
                _in_current_stream = _at_state and agent.id in getattr(_at_state, 'current_stream_agents', set())

                if _in_current_stream:
                    # Result will appear as a skill card in the current turn — no notification needed
                    pass
                elif _is_busy:
                    # Model is mid-stream but agent wasn't spawned this turn → queue for next turn
                    notification_queue.push(agent.chat_id, AgentEvent(
                        type=event_type,
                        agent_id=agent.id,
                        data=_evt_data,
                    ))
                # else: not busy → auto_turn.on_agent_done/on_agent_failed handles delivery exclusively

                # Always persist into skill_events for history replay
                try:
                    from server.database import append_skill_event
                    append_skill_event(agent.chat_id, {
                        "type": "agent_result",
                        "agent_id": agent.id,
                        "ok": event_type == "agent_completed",
                        "data": _evt_data,
                    })
                except Exception:
                    pass

            # Save full agent output to disk (non-blocking)
            await asyncio.to_thread(self._save_agent_output, agent)

    # ------------------------------------------------------------------
    # Waiting (collect mode)
    # ------------------------------------------------------------------

    def _to_awaitable(self, task):
        """Convert a task/future to an asyncio-awaitable."""
        if isinstance(task, concurrent.futures.Future):
            return asyncio.wrap_future(task)
        return task

    async def wait(self, agent_id: str, timeout: float | None = None) -> AgentResult:
        """Wait for a single agent to finish."""
        task = self._tasks.get(agent_id)
        if task and not task.done():
            await asyncio.wait_for(self._to_awaitable(task), timeout=timeout)
        return self._to_result(agent_id)

    async def wait_all(self, agent_ids: list[str], timeout: float | None = None) -> list[AgentResult]:
        """Wait for multiple agents. Returns results in order."""
        tasks = [self._to_awaitable(self._tasks[aid]) for aid in agent_ids if aid in self._tasks and not self._tasks[aid].done()]
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout)
        return [self._to_result(aid) for aid in agent_ids if aid in self._agents]

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    async def kill(self, agent_id: str) -> None:
        """Cancel a running agent."""
        agent = self._agents.get(agent_id)
        if agent:
            agent.cancelled = True  # Checked between tool calls in the loop
        task = self._tasks.get(agent_id)
        if task and not task.done():
            task.cancel()
        if agent:
            agent.mark_failed("Killed by orchestrator")
            from server.database import update_agent_status
            update_agent_status(agent_id, "killed", error="Killed by orchestrator")

    def list_agents(self, chat_id: str | None = None) -> list[Agent]:
        agents = list(self._agents.values())
        if chat_id:
            agents = [a for a in agents if a.chat_id == chat_id]
        return agents

    def get_agent(self, agent_id: str) -> Agent | None:
        return self._agents.get(agent_id)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _emit(self, chat_id: str | None, event: AgentEvent) -> None:
        """Push event to SSE clients via callback."""
        if chat_id and self._event_callback:
            payload = {"type": event.type, "agent_id": event.agent_id, "data": event.data}
            try:
                await self._event_callback(chat_id, payload)
            except Exception as exc:
                logger.debug("SSE emit failed: %s", exc)

    @staticmethod
    async def _persist_failure(agent_id: str, message: str) -> None:
        """Persist a failure message into the agent's DB conversation history."""
        try:
            from server.database import add_agent_message
            add_agent_message(agent_id, "system", message)
        except Exception as exc:
            logger.debug("Failed to persist agent failure msg: %s", exc)

    @staticmethod
    def _save_agent_output(agent: Agent) -> None:
        """Save agent output to two files:
        - <id>.md: final result or error only
        - <id>_conversation.md: full raw conversation (including system prompt)
        """
        try:
            _AGENT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

            # --- File 1: result only ---
            result_lines: list[str] = [
                f"# Agent {agent.id} — {agent.role}",
                "",
                f"- **Task:** {agent.task}",
                f"- **Model:** {agent.model}",
                f"- **Status:** {agent.status.value}",
                f"- **Duration:** {agent.duration:.1f}s",
                f"- **Words:** {agent.word_count}",
                f"- **Skills:** {', '.join(agent.skills_used) or 'none'}",
            ]
            if agent.error:
                result_lines.append(f"- **Error:** {agent.error}")
            result_lines.append("")
            result_lines.append("## Result" if agent.result else "## Error")
            result_lines.append("")
            result_lines.append(agent.result or agent.error or "No output.")
            result_lines.append("")

            out_path = _AGENT_OUTPUT_DIR / f"{agent.id}.md"
            out_path.write_text("\n".join(result_lines), encoding="utf-8")

            # --- File 2: full conversation (skip system messages) ---
            conv_lines: list[str] = [
                f"# Agent {agent.id} — Conversation",
                "",
                f"- **Role:** {agent.role}",
                f"- **Task:** {agent.task}",
                "",
            ]
            for msg in agent.messages:
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                conv_lines.append(f"### [{role}]")
                conv_lines.append("")
                conv_lines.append(content)
                conv_lines.append("")

            # Append skip reasons if any
            if agent.todos and agent.todos.skip_reasons:
                conv_lines.append("---")
                conv_lines.append("")
                conv_lines.append("## Skipped Tasks")
                conv_lines.append("")
                for todo_id, reason in agent.todos.skip_reasons:
                    # Find the original content for this todo_id
                    skipped_content = "unknown"
                    for t in agent.todos.todos:
                        if t.id == todo_id:
                            skipped_content = t.content
                            break
                    conv_lines.append(f"- **Task #{todo_id}** \"{skipped_content}\": {reason}")
                conv_lines.append("")

            conv_path = _AGENT_OUTPUT_DIR / f"{agent.id}_conversation.md"
            conv_path.write_text("\n".join(conv_lines), encoding="utf-8")

            logger.info("Saved agent output: %s, %s", out_path, conv_path)
        except Exception as exc:
            logger.debug("Failed to save agent output: %s", exc)

    def _to_result(self, agent_id: str) -> AgentResult:
        agent = self._agents[agent_id]
        return AgentResult(
            agent_id=agent.id,
            role=agent.role,
            status=agent.status,
            summary=agent.result or agent.error or "",
            tokens_used=agent.tokens_used,
            error=agent.error,
            duration_seconds=agent.duration,
            skills_used=agent.skills_used,
        )


# ------------------------------------------------------------------
# Module-level singleton
# ------------------------------------------------------------------

_runtime: AgentRuntime | None = None


def get_runtime() -> AgentRuntime:
    """Get or create the AgentRuntime singleton."""
    global _runtime
    if _runtime is None:
        _runtime = AgentRuntime()
    return _runtime
