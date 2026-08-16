
"""Cookbook state persistence — tracks downloads, servers, and settings."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

_STATE_FILE = Path(__file__).resolve().parent.parent.parent / "system" / "cookbook_state.json"
_MODELS_DIR = Path(__file__).resolve().parent.parent.parent / "system" / "models" / "llm"
_LOGS_DIR = Path(__file__).resolve().parent.parent.parent / "system" / "cookbook_logs"


@dataclass
class DownloadTask:
    id: str
    repo_id: str
    filename: str = ""
    include: str = ""
    local_dir: str = ""
    status: str = "pending"  # pending, downloading, done, failed, cancelled
    progress: float = 0.0
    bytes_downloaded: int = 0
    total_bytes: int = 0
    speed_bps: float = 0.0
    error: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0


@dataclass
class ServeTask:
    id: str
    model_path: str
    model_label: str = ""
    backend: str = "llama-server"  # llama-server, vllm, ollama
    host: str = "127.0.0.1"
    port: int = 8080
    pid: int = 0
    status: str = "starting"  # starting, running, stopped, failed
    ctx_size: int = 4096
    threads: int = 0  # 0 = auto
    gpu_layers: int = 0
    extra_args: str = ""
    started_at: float = field(default_factory=time.time)
    stopped_at: float = 0.0
    error: str = ""


@dataclass
class CookbookSettings:
    hf_token: str = ""
    models_dir: str = ""
    llama_server_bin: str = ""  # auto-detected or user-set path to llama-server
    default_port: int = 8080
    default_ctx: int = 4096
    default_threads: int = 0
    default_gpu_layers: int = 0
    auto_register: bool = True  # auto-add served models to .custom_models.json


class CookbookState:
    """Central state manager for the Cookbook subsystem."""

    def __init__(self) -> None:
        self.downloads: list[DownloadTask] = []
        self.servers: list[ServeTask] = []
        self.settings: CookbookSettings = CookbookSettings()
        self._load()

    @property
    def models_dir(self) -> Path:
        if self.settings.models_dir:
            return Path(self.settings.models_dir)
        return _MODELS_DIR

    @property
    def logs_dir(self) -> Path:
        return _LOGS_DIR

    def _load(self) -> None:
        if not _STATE_FILE.exists():
            return
        try:
            raw = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return

        for d in raw.get("downloads", []):
            # Only restore active downloads — finished/failed/cancelled are
            # transient UI feedback and shouldn't survive restarts.
            if d.get("status") in ("downloading", "pending"):
                self.downloads.append(DownloadTask(**{k: v for k, v in d.items() if k in DownloadTask.__dataclass_fields__}))
        for s in raw.get("servers", []):
            self.servers.append(ServeTask(**{k: v for k, v in s.items() if k in ServeTask.__dataclass_fields__}))

        settings_raw = raw.get("settings", {})
        for k, v in settings_raw.items():
            if hasattr(self.settings, k):
                setattr(self.settings, k, v)

    def save(self) -> None:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Only persist active downloads — done/failed/cancelled are transient
        # UI feedback. They stay in memory for current-session API responses
        # but don't survive restarts.
        active_downloads = [d for d in self.downloads if d.status in ("downloading", "pending")]
        data = {
            "downloads": [asdict(d) for d in active_downloads],
            "servers": [asdict(s) for s in self.servers],
            "settings": asdict(self.settings),
        }
        _STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def get_download(self, task_id: str) -> DownloadTask | None:
        return next((d for d in self.downloads if d.id == task_id), None)

    def get_server(self, task_id: str) -> ServeTask | None:
        return next((s for s in self.servers if s.id == task_id), None)

    def active_servers(self) -> list[ServeTask]:
        return [s for s in self.servers if s.status == "running"]

    def active_downloads(self) -> list[DownloadTask]:
        return [d for d in self.downloads if d.status == "downloading"]

    def cleanup_stale(self) -> None:
        """Mark servers as stopped if their PID is no longer alive."""
        import os
        changed = False
        for s in self.servers:
            if s.status == "running" and s.pid > 0:
                try:
                    os.kill(s.pid, 0)
                except (ProcessLookupError, PermissionError):
                    s.status = "stopped"
                    s.stopped_at = time.time()
                    changed = True
        if changed:
            self.save()


_state: CookbookState | None = None


def get_state() -> CookbookState:
    global _state
    if _state is None:
        _state = CookbookState()
    return _state
