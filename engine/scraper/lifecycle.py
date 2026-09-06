"""Browser lifecycle management: engine loading, start/stop, probe, kill."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import sys
import types
import uuid
from pathlib import Path
from typing import Any

from engine.config import BROWSER_SCRAPER_DATA_DIR

from .loader import _load_py_module, _accepts_arg
from .settings import (
    DEFAULT_ENGINE_TYPE,
    DEFAULT_SETTINGS,
    ENGINE_REGISTRY,
    _load_settings,
    _resolve_engine_path,
)

logger = logging.getLogger("sable.scraper")


class ScraperLifecycle:
    """Mixin: browser engine lifecycle (load, start, stop, probe, kill)."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.engine: Any | None = None
        self.loaded_path: str | None = None
        self.active_chat_id: str | None = None

    async def reset(self) -> None:
        await self.stop()

    async def stop(self, *, kill_browser: bool = False) -> None:
        async with self._lock:
            await self._cleanup_engine(kill_browser=kill_browser)

    async def _cleanup_engine(self, *, kill_browser: bool = False) -> None:
        engine = self.engine
        if engine is None:
            return

        if not kill_browser:
            return

        self.engine = None
        self.loaded_path = None
        self.active_chat_id = None

        cleanup = getattr(engine, "cleanup", None)
        if cleanup is None:
            return

        try:
            result = cleanup()
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.warning("Scraper engine cleanup failed: %s", exc)

    def _find_engine_class(self, module: types.ModuleType) -> type:
        cls = getattr(module, "GhostChat", None)
        if cls is not None:
            return cls

        for value in vars(module).values():
            if (
                inspect.isclass(value)
                and hasattr(value, "send_msg")
                and hasattr(value, "get_response")
            ):
                return value

        raise RuntimeError("No GhostChat-like class found in engine module")

    def _load_engine_class(self, engine_path: Path) -> type:
        engine_path = engine_path.resolve()
        if not engine_path.exists():
            raise RuntimeError(f"Scraper engine file not found: {engine_path}")

        engine_dir = engine_path.parent
        digest = hashlib.sha1(str(engine_path).encode("utf-8")).hexdigest()[:10]
        module_name = f"sable_scraper_engine_{digest}"

        cached = sys.modules.get(module_name)
        if cached is not None:
            return self._find_engine_class(cached)

        saved_top_level = {
            "config": sys.modules.get("config"),
            "exceptions": sys.modules.get("exceptions"),
        }

        try:
            exceptions_path = engine_dir / "exceptions.py"
            if exceptions_path.exists():
                exceptions_module = _load_py_module(f"sable_scraper_exceptions_{digest}", exceptions_path)
            else:
                exceptions_module = types.ModuleType("exceptions")
                exceptions_module.ResponseCaptureError = type(
                    "ResponseCaptureError",
                    (RuntimeError,),
                    {"__doc__": "Fallback ResponseCaptureError"},
                )
            sys.modules["exceptions"] = exceptions_module

            config_path = engine_dir / "config.py"
            if not config_path.exists():
                raise RuntimeError(
                    f"No config.py found beside {engine_path}. "
                    "Copy/create a GhostChat-style config.py + platforms.json first."
                )
            config_module = _load_py_module(f"sable_scraper_config_{digest}", config_path)

            platform_config = getattr(config_module, "PLATFORMS_CONFIG", None)
            if isinstance(platform_config, dict):
                platform_config["use_obsidian"] = False

            sys.modules["config"] = config_module

            if str(engine_dir) not in sys.path:
                sys.path.insert(0, str(engine_dir))

            engine_module = _load_py_module(module_name, engine_path)
            return self._find_engine_class(engine_module)
        finally:
            for name, old_module in saved_top_level.items():
                if old_module is not None:
                    sys.modules[name] = old_module
                else:
                    sys.modules.pop(name, None)

    def _instantiate_engine(self, cls: type, settings: dict[str, Any]) -> Any:
        kwargs = {
            "port": int(settings.get("port", 9333)),
            "viewer": not bool(settings.get("headless", False)),
            "show_thoughts": bool(settings.get("show_thoughts", True)),
        }

        try:
            return cls(**kwargs)
        except TypeError:
            pass

        try:
            return cls(port=kwargs["port"], viewer=kwargs["viewer"])
        except TypeError:
            pass

        return cls()

    async def _ensure_engine(self, settings: dict[str, Any]) -> Any:
        engine_type = settings.get("engine_type", DEFAULT_ENGINE_TYPE)
        engine_path = _resolve_engine_path(engine_type)

        if self.engine is not None and self.loaded_path == engine_path:
            return self.engine

        await self._cleanup_engine()

        cls = self._load_engine_class(Path(engine_path))
        engine = self._instantiate_engine(cls, settings)

        try:
            engine.user_data_dir = str(BROWSER_SCRAPER_DATA_DIR)
        except Exception:
            pass

        try:
            launch = getattr(engine, "launch_chrome", None)
            if launch is not None:
                await launch()
            await engine.connect()
        except SystemExit as exc:
            raise RuntimeError(f"Browser engine exited during startup: {exc}") from exc

        self.engine = engine
        self.loaded_path = engine_path

        # Diagnostics: passively track engine session
        try:
            from .diagnostics import get_monitor
            monitor = get_monitor()
            settings_diag = _load_settings()
            import asyncio as _aio
            sid = _aio.get_event_loop().run_until_complete(
                monitor.register_session(
                    settings_diag.get("engine_type", DEFAULT_ENGINE_TYPE),
                    metadata={"cdp_port": getattr(engine, "port", None)},
                )
            )
            self._diag_session_id = sid
        except Exception:
            self._diag_session_id = None

        return engine

    async def _is_browser_alive(self, engine: Any) -> bool:
        """Quick CDP liveness probe — returns False if browser was closed externally."""
        port = getattr(engine, "port", None)
        if port is None:
            return True
        import socket
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1.0)
                return s.connect_ex(("127.0.0.1", int(port))) == 0
        except Exception:
            return False

    async def _probe_existing_session(self, settings: dict[str, Any]) -> bool:
        """Check if a headed browser is already running on the configured CDP port."""
        import os
        import socket
        import urllib.request

        port = settings.get("port", DEFAULT_SETTINGS["port"])
        user_data_dir = str(BROWSER_SCRAPER_DATA_DIR)

        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1.0)
                if s.connect_ex(("127.0.0.1", port)) != 0:
                    return False
        except Exception:
            return False

        try:
            url = f"http://127.0.0.1:{port}/json/version"
            req = urllib.request.Request(url, headers={"User-Agent": "GhostChat"})
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                data = json.loads(resp.read().decode())
            remote_profile = data.get("userDataDir", "") or data.get("profile-path", "")
            if os.path.realpath(remote_profile) != os.path.realpath(user_data_dir):
                return False
            cmd_line = data.get("BrowserCommandLine", "")
            if "--headless" in cmd_line:
                return False
        except Exception:
            return False

        engine_type = settings.get("engine_type", DEFAULT_ENGINE_TYPE)
        engine_path = _resolve_engine_path(engine_type)
        try:
            cls = self._load_engine_class(Path(engine_path))
            engine = self._instantiate_engine(cls, settings)
            try:
                engine.user_data_dir = user_data_dir
            except Exception:
                pass
            await engine.connect()
            self.engine = engine
            self.loaded_path = engine_path
            logger.info("Reconnected to existing headed browser on port %d", port)
            return True
        except Exception as exc:
            logger.warning("Failed to reconnect to existing session: %s", exc)
            return False

    async def prelaunch(self) -> dict[str, Any]:
        """Pre-launch the browser when scraper mode is enabled."""
        settings = _load_settings()
        if not settings.get("enabled"):
            return {"status": "skipped", "message": "Scraper is disabled"}

        try:
            async with self._lock:
                engine_type = settings.get("engine_type", DEFAULT_ENGINE_TYPE)
                engine_path = _resolve_engine_path(engine_type)
                if self.engine is not None and self.loaded_path == engine_path:
                    return {"status": "ok", "message": "Browser already running"}
                if await self._probe_existing_session(settings):
                    return {"status": "ok", "message": "Reconnected to existing browser"}
                await self._ensure_engine(settings)
            return {"status": "ok", "message": "Browser launched and connected"}
        except Exception as exc:
            logger.exception("Browser prelaunch failed")
            return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}

    async def switch_model(self, model_type: str) -> dict[str, Any]:
        """Switch the active model type on the browser engine."""
        settings = _load_settings()
        if not settings.get("enabled"):
            return {"status": "error", "message": "Scraper is disabled"}

        try:
            async with self._lock:
                engine = await self._ensure_engine(settings)
                switch = getattr(engine, "switch_model", None)
                if switch is None:
                    return {"status": "error", "message": "Engine does not support model switching"}
                ok = await switch(model_type)
                if ok:
                    self.active_chat_id = None
                    return {"status": "ok", "model_type": model_type}
                return {"status": "error", "message": f"Could not switch to {model_type}"}
        except Exception as exc:
            logger.exception("Model switch failed")
            return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}

    async def get_ui_metadata(self) -> dict[str, Any]:
        """Return engine-specific UI metadata (models, thinking modes)."""
        settings = _load_settings()
        try:
            async with self._lock:
                engine = await self._ensure_engine(settings)
                meta_fn = getattr(engine, "get_ui_metadata", None)
                if meta_fn:
                    return meta_fn()
        except Exception as exc:
            logger.warning("Could not get UI metadata: %s", exc)

        # Fallback based on engine_type from settings
        engine_type = settings.get("engine_type", DEFAULT_ENGINE_TYPE)
        if engine_type == "deepseek":
            return {
                "models": [
                    {"id": "default", "label": "Instant"},
                    {"id": "expert", "label": "Expert"},
                    {"id": "vision", "label": "Vision"},
                ],
                "thinking_modes": [
                    {"id": "fast", "label": "Fast"},
                    {"id": "deepthink", "label": "DeepThink"},
                ],
            }
        elif engine_type == "chatgpt":
            return {
                "models": [{"id": "default", "label": "ChatGPT"}],
                "thinking_modes": [
                    {"id": "fast", "label": "Fast"},
                    {"id": "thinking", "label": "Thinking"},
                ],
            }
        return {
            "models": [{"id": "default", "label": engine_type.title()}],
            "thinking_modes": [],
        }

    async def get_session_info(self) -> dict[str, Any]:
        """Return info about the active browser session for the settings UI."""
        engine = self.engine
        if engine is None:
            return {"active": False}

        settings = _load_settings()
        alive = await self._is_browser_alive(engine)

        pid: int | None = None
        chrome_proc = getattr(engine, "chrome_process", None)
        if chrome_proc is not None:
            try:
                pid = chrome_proc.pid
            except Exception:
                pass

        page_url: str | None = None
        page = getattr(engine, "page", None)
        if page is not None:
            try:
                page_url = page.url
            except Exception:
                pass

        return {
            "active": True,
            "alive": alive,
            "chat_id": self.active_chat_id,
            "engine_type": settings.get("engine_type", DEFAULT_ENGINE_TYPE),
            "cdp_port": getattr(engine, "port", None),
            "chrome_pid": pid,
            "page_url": page_url,
            "headless": bool(settings.get("headless", False)),
        }

    async def kill_session(self) -> dict[str, Any]:
        """Gracefully stop the browser, escalating to SIGKILL only if needed."""
        import os
        import signal

        engine = self.engine
        killed_pid: int | None = None

        if engine is not None:
            cleanup = getattr(engine, "cleanup", None)
            if cleanup is not None:
                try:
                    result = cleanup()
                    if inspect.isawaitable(result):
                        await asyncio.wait_for(result, timeout=5.0)
                except Exception:
                    pass

            chrome_proc = getattr(engine, "chrome_process", None)
            if chrome_proc is not None:
                try:
                    from engine.process_utils import kill_process_tree
                    pid = chrome_proc.pid
                    os.kill(pid, 0)  # check alive
                    kill_process_tree(pid, sig=signal.SIGTERM)
                    killed_pid = pid
                    await asyncio.sleep(3.0)
                    try:
                        os.kill(pid, 0)
                        kill_process_tree(pid, sig=signal.SIGKILL)
                    except (ProcessLookupError, OSError):
                        pass
                except (ProcessLookupError, PermissionError, OSError):
                    pass

        await self.stop(kill_browser=True)
        return {"status": "ok", "killed_pid": killed_pid}
