
"""Model serving lifecycle — llama-server process management."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import time
import uuid
from pathlib import Path

from engine.cookbook.state import ServeTask, get_state

logger = logging.getLogger(__name__)

_LLAMA_SERVER_BIN = "llama-server"


class ServeError(Exception):
    pass


class ServeManager:
    """Manages llama-server processes for local model inference."""

    def __init__(self) -> None:
        self._processes: dict[str, subprocess.Popen] = {}

    def _find_binary(self) -> str:
        """Locate llama-server binary."""
        path = shutil.which(_LLAMA_SERVER_BIN)
        if not path:
            raise ServeError(
                "llama-server not found. Install with: sudo pacman -S llama-cpp"
            )
        return path

    def _build_command(self, task: ServeTask) -> list[str]:
        """Build the llama-server command line."""
        binary = self._find_binary()
        cmd = [
            binary,
            "--model", task.model_path,
            "--host", task.host,
            "--port", str(task.port),
            "--ctx-size", str(task.ctx_size),
        ]

        if task.threads > 0:
            cmd.extend(["--threads", str(task.threads)])

        if task.gpu_layers > 0:
            cmd.extend(["--n-gpu-layers", str(task.gpu_layers)])

        # Enable OpenAI-compatible API
        cmd.append("--api-key")
        cmd.append("sable-local")

        # Use model's built-in Jinja2 chat template (required for tool calling)
        cmd.append("--jinja")

        # Extra user args
        if task.extra_args:
            cmd.extend(task.extra_args.split())

        return cmd

    def _log_path(self, task_id: str) -> Path:
        logs_dir = get_state().logs_dir
        logs_dir.mkdir(parents=True, exist_ok=True)
        return logs_dir / f"{task_id}.log"

    async def start_server(
        self,
        model_path: str,
        *,
        model_label: str = "",
        host: str = "127.0.0.1",
        port: int | None = None,
        ctx_size: int | None = None,
        threads: int = 0,
        gpu_layers: int = 0,
        extra_args: str = "",
    ) -> ServeTask:
        """Start a new llama-server instance."""
        model = Path(model_path)
        if not model.exists():
            raise ServeError(f"Model file not found: {model_path}")
        if not model.suffix == ".gguf":
            raise ServeError(f"Not a GGUF file: {model_path}")

        state = get_state()
        port = port or state.settings.default_port or 8080
        ctx_size = ctx_size or state.settings.default_ctx or 4096

        # Check port availability
        if self._port_in_use(port):
            raise ServeError(f"Port {port} is already in use")

        task_id = f"serve-{uuid.uuid4().hex[:8]}"
        task = ServeTask(
            id=task_id,
            model_path=str(model),
            model_label=model_label or model.stem,
            host=host,
            port=port,
            ctx_size=ctx_size,
            threads=threads,
            gpu_layers=gpu_layers,
            extra_args=extra_args,
        )

        cmd = self._build_command(task)
        log_file = self._log_path(task_id)

        try:
            with open(log_file, "w") as lf:
                lf.write(f"[sable-cookbook] Starting: {' '.join(cmd)}\n")
                lf.write(f"[sable-cookbook] Time: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
                lf.flush()

                proc = subprocess.Popen(
                    cmd,
                    stdout=lf,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,  # Detach from parent
                )

            task.pid = proc.pid
            task.status = "starting"
            self._processes[task_id] = proc

        except OSError as exc:
            raise ServeError(f"Failed to start llama-server: {exc}")

        state.servers.append(task)
        state.save()

        # Wait briefly and check if it's actually running
        await asyncio.sleep(1.5)
        if proc.poll() is not None:
            task.status = "failed"
            task.error = f"Process exited immediately with code {proc.returncode}. Check logs."
            state.save()
            raise ServeError(task.error)

        task.status = "running"
        state.save()
        logger.info("Server started: %s (pid=%d, port=%d)", task.model_label, task.pid, task.port)

        # Auto-register in custom models if enabled
        if state.settings.auto_register:
            self._register_model(task)

        return task

    def stop_server(self, task_id: str) -> bool:
        """Stop a running server."""
        state = get_state()
        task = state.get_server(task_id)
        if not task or task.status != "running":
            return False

        # Try graceful shutdown
        try:
            os.kill(task.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

        # Wait up to 5s for graceful exit
        proc = self._processes.get(task_id)
        if proc:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.kill(task.pid, signal.SIGKILL)
            self._processes.pop(task_id, None)

        task.status = "stopped"
        task.stopped_at = time.time()
        state.save()

        # Unregister from custom models
        if state.settings.auto_register:
            self._unregister_model(task)

        logger.info("Server stopped: %s", task.model_label)
        return True

    def get_server_status(self, task_id: str) -> dict:
        """Get detailed status of a server."""
        state = get_state()
        task = state.get_server(task_id)
        if not task:
            return {"error": "not found"}

        alive = False
        if task.pid > 0:
            try:
                os.kill(task.pid, 0)
                alive = True
            except (ProcessLookupError, PermissionError):
                alive = False

        return {
            "id": task.id,
            "model": task.model_label,
            "status": task.status if alive else "stopped",
            "pid": task.pid,
            "port": task.port,
            "endpoint": f"http://{task.host}:{task.port}/v1",
            "alive": alive,
        }

    def tail_logs(self, task_id: str, lines: int = 50) -> str:
        """Read the last N lines of a server's log."""
        log_path = self._log_path(task_id)
        if not log_path.exists():
            return ""
        try:
            content = log_path.read_text(encoding="utf-8", errors="replace")
            return "\n".join(content.splitlines()[-lines:])
        except OSError:
            return ""

    def _port_in_use(self, port: int) -> bool:
        """Check if a port is already bound."""
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return False
            except OSError:
                return True

    def _register_model(self, task: ServeTask) -> None:
        """Add served model to Sable's .custom_models.json."""
        custom_models_path = Path(__file__).resolve().parent.parent.parent / "system" / ".custom_models.json"
        try:
            models = json.loads(custom_models_path.read_text()) if custom_models_path.exists() else []
        except (json.JSONDecodeError, OSError):
            models = []

        model_id = f"local/{task.model_label.lower().replace(' ', '-')}"

        # Don't duplicate
        if any(m.get("id") == model_id for m in models):
            return

        models.append({
            "id": model_id,
            "label": f"⚡ {task.model_label}",
            "api_backend": "local",
            "api_model_type": task.model_label,
            "local_endpoint": f"http://{task.host}:{task.port}/v1",
            "capabilities": {"image": False, "video": False, "document": False, "audio": False},
            "thinking_modes": [
                {"id": "fast", "label": "Fast", "thinking_enabled": False},
                {"id": "thinking", "label": "Thinking", "thinking_enabled": True},
            ],
            "_custom": True,
            "_cookbook": True,
        })

        custom_models_path.write_text(json.dumps(models, indent=2), encoding="utf-8")
        logger.info("Registered model in custom_models: %s", model_id)

    def _unregister_model(self, task: ServeTask) -> None:
        """Remove served model from .custom_models.json."""
        custom_models_path = Path(__file__).resolve().parent.parent.parent / "system" / ".custom_models.json"
        try:
            models = json.loads(custom_models_path.read_text()) if custom_models_path.exists() else []
        except (json.JSONDecodeError, OSError):
            return

        model_id = f"local/{task.model_label.lower().replace(' ', '-')}"
        models = [m for m in models if m.get("id") != model_id]
        custom_models_path.write_text(json.dumps(models, indent=2), encoding="utf-8")

    def cleanup_stale(self) -> None:
        """Check all 'running' servers and mark dead ones as stopped."""
        state = get_state()
        state.cleanup_stale()
