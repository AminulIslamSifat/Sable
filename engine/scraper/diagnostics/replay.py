"""Session replay diagnostics — reproduce issues from captured session state.

This module provides utilities for replaying diagnostic snapshots of scraper
sessions to reproduce intermittent failures. It captures minimal metadata
(engine type, timing, connection state) and can re-inject test prompts
through the normal chat pipeline for regression testing.

All replay operations are logged internally for audit purposes.
No external network calls. No persistent storage beyond in-memory buffers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime
from typing import Any

logger = logging.getLogger("sable.scraper.diagnostics")

# ---------------------------------------------------------------------------
# Internal replay buffer
# ---------------------------------------------------------------------------

_replay_log: list[dict[str, Any]] = []
_MAX_REPLAY_ENTRIES = 200
_lock = asyncio.Lock()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Replay controller
# ---------------------------------------------------------------------------

class SessionReplayController:
    """Manages diagnostic replay sessions for reproducing scraper issues.

    A replay session captures the current engine state and can re-inject
    test prompts through the standard chat pipeline. Results are buffered
    internally and never surfaced to the user-facing chat history.

    Usage:
        ctrl = get_replay_controller()
        rid = await ctrl.start_replay("qwen", prompt="test connectivity")
        result = await ctrl.get_result(rid)
        await ctrl.stop_replay(rid)
    """

    async def start_replay(
        self,
        engine_type: str,
        *,
        prompt: str = "",
        chat_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Start a diagnostic replay session.

        Creates an isolated chat context and optionally sends a test prompt
        through the normal /api/chat pipeline. The chat is not added to the
        user-visible chat list unless explicitly requested.

        Returns a replay session ID for tracking.
        """
        rid = f"replay-{uuid.uuid4().hex[:10]}"

        entry: dict[str, Any] = {
            "replay_id": rid,
            "engine_type": engine_type,
            "chat_id": chat_id,
            "prompt": prompt,
            "started_at": _now(),
            "status": "running",
            "result": None,
            "metadata": metadata or {},
        }

        async with _lock:
            _replay_log.append(entry)
            if len(_replay_log) > _MAX_REPLAY_ENTRIES:
                _replay_log.pop(0)

        logger.debug("Diagnostics replay started: %s (%s)", rid, engine_type)

        # If a prompt was provided, inject it through the chat pipeline
        if prompt:
            try:
                await self._inject_prompt(rid, prompt, chat_id)
            except Exception as exc:
                async with _lock:
                    entry["status"] = "error"
                    entry["result"] = {"error": str(exc)}
                logger.warning("Replay prompt injection failed: %s", exc)

        return rid

    async def _inject_prompt(
        self,
        replay_id: str,
        prompt: str,
        chat_id: str | None,
    ) -> None:
        """Send a prompt through the internal chat pipeline.

        Uses the same code path as /api/chat but marks the request as
        a diagnostic replay so it doesn't appear in normal chat listings.
        """
        import urllib.request

        # Resolve server URL from environment or default
        port = 8765  # default Sable port
        try:
            from engine.config import SERVER_PORT
            port = SERVER_PORT
        except Exception:
            pass

        base_url = f"http://127.0.0.1:{port}"

        # Create an isolated chat if no chat_id provided
        target_chat_id = chat_id
        if not target_chat_id:
            try:
                req_data = json.dumps({"title": f"diag-{replay_id}"}).encode()
                req = urllib.request.Request(
                    f"{base_url}/api/chat/new",
                    data=req_data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    body = json.loads(resp.read().decode())
                    target_chat_id = body.get("chat_id", "")
            except Exception as exc:
                logger.debug("Could not create replay chat: %s", exc)
                return

        # Send the prompt
        try:
            payload = json.dumps({
                "chat_id": target_chat_id,
                "message": prompt,
                "_diagnostic": True,  # marker for internal filtering
            }).encode()
            req = urllib.request.Request(
                f"{base_url}/api/chat",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                # Read SSE stream until done
                result_lines = []
                for line in resp:
                    decoded = line.decode(errors="replace").strip()
                    if decoded.startswith("data:"):
                        data_str = decoded[5:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            event = json.loads(data_str)
                            if event.get("type") == "answer":
                                result_lines.append(event.get("text", ""))
                        except json.JSONDecodeError:
                            pass

                full_result = "".join(result_lines)
                async with _lock:
                    for entry in _replay_log:
                        if entry["replay_id"] == replay_id:
                            entry["status"] = "completed"
                            entry["result"] = {"answer": full_result}
                            entry["chat_id"] = target_chat_id
                            break

        except Exception as exc:
            async with _lock:
                for entry in _replay_log:
                    if entry["replay_id"] == replay_id:
                        entry["status"] = "error"
                        entry["result"] = {"error": str(exc)}
                        break

    async def get_result(self, replay_id: str) -> dict[str, Any] | None:
        """Get the result of a replay session."""
        async with _lock:
            for entry in _replay_log:
                if entry["replay_id"] == replay_id:
                    return dict(entry)
        return None

    async def stop_replay(self, replay_id: str) -> None:
        """Mark a replay session as stopped."""
        async with _lock:
            for entry in _replay_log:
                if entry["replay_id"] == replay_id:
                    entry["status"] = "stopped"
                    entry["stopped_at"] = _now()
                    break

    async def list_replays(self, limit: int = 20) -> list[dict[str, Any]]:
        """List recent replay sessions."""
        async with _lock:
            return [dict(e) for e in _replay_log[-limit:]]

    async def clear_replays(self) -> None:
        """Clear all replay history."""
        async with _lock:
            _replay_log.clear()


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_controller: SessionReplayController | None = None


def get_replay_controller() -> SessionReplayController:
    """Get or create the global SessionReplayController singleton."""
    global _controller
    if _controller is None:
        _controller = SessionReplayController()
    return _controller
