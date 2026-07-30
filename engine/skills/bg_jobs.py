
"""Background job manager — namespaced process tracking."""

from __future__ import annotations

import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Generator

from engine.skills.events import end_event, output_event

RESULT_PREVIEW_CHARS = 20_000


class BackgroundJobManager:
    """Manages background processes with optional session namespacing.

    Each job gets a UUID-based log file in /tmp. Jobs are tracked in-memory
    (lost on server restart — acceptable for dev tooling).
    """

    def __init__(self) -> None:
        # namespace -> {pid -> job_info}
        self._jobs: dict[str, dict[int, dict[str, Any]]] = {}

    def start(
        self,
        tag_id: str,
        name: str,
        command: str,
        namespace: str = "default",
    ) -> Generator[dict[str, Any], None, None]:
        """Launch a background process and track it."""
        started = time.time()

        if not command.strip():
            yield output_event(tag_id, "No command provided\n", "stderr")
            yield end_event(tag_id, name, False, started, error="Empty command")
            return

        log_path = Path("/tmp") / f"ghost_bg_{uuid.uuid4().hex}.log"
        log_file = log_path.open("w", encoding="utf-8")
        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=str(Path.home()),
                start_new_session=True,
            )
        finally:
            log_file.close()

        pid = proc.pid
        ns = self._jobs.setdefault(namespace, {})
        ns[pid] = {
            "pid": pid,
            "command": command,
            "log": str(log_path),
            "started": time.time(),
            "status": "running",
            "namespace": namespace,
        }

        yield output_event(tag_id, f"Started background job {pid}\nLog: {log_path}\n$ {command}\n")
        yield end_event(tag_id, name, True, started, {"pid": pid, "log": str(log_path), "command": command})

    def check(
        self,
        tag_id: str,
        name: str,
        pid: int | None = None,
        namespace: str = "default",
    ) -> Generator[dict[str, Any], None, None]:
        """Check a specific job or list all jobs in a namespace."""
        started = time.time()
        ns = self._jobs.get(namespace, {})

        if pid is not None:
            info = ns.get(pid, {})
            log_path = Path(info.get("log", f"/tmp/ghost_bg_{pid}.log"))
            running = Path(f"/proc/{pid}").exists()

            tail = ""
            if log_path.exists():
                try:
                    data = log_path.read_text(errors="replace")
                    tail = data[-RESULT_PREVIEW_CHARS:]
                except Exception as exc:
                    tail = f"Could not read log: {exc}"
            if not tail:
                tail = "(no log output found)"

            yield output_event(tag_id, tail + "\n")
            yield end_event(tag_id, name, True, started, {
                "pid": pid,
                "running": running,
                "log": str(log_path),
                "command": info.get("command"),
            })
            return

        # List all jobs
        if not ns:
            yield output_event(tag_id, "No background jobs tracked.\n")
            yield end_event(tag_id, name, True, started, {"jobs": []})
            return

        jobs = []
        for job_pid, info in ns.items():
            running = Path(f"/proc/{job_pid}").exists()
            info["status"] = "running" if running else "exited"
            jobs.append(info)
            yield output_event(tag_id, f"{job_pid} [{info['status']}] {info.get('command', '')} -> {info.get('log', '')}\n")

        yield end_event(tag_id, name, True, started, {"jobs": jobs})

    def get_all(self, namespace: str = "default") -> list[dict[str, Any]]:
        """Return all tracked jobs for a namespace (for API listing)."""
        ns = self._jobs.get(namespace, {})
        for info in ns.values():
            info["status"] = "running" if Path(f"/proc/{info['pid']}").exists() else "exited"
        return list(ns.values())
