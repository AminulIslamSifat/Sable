"""Browser web-scraper backend for Sable.

Wraps GhostChat-style browser engines (Maria qwen_engine.py, GhostChat
deepseek_engine.py, etc.) behind the same async event interface used by
Sable's normal HTTP ChatService.

The scraper is disabled by default and can be toggled from the header switch.
When enabled, it runs the browser in headed mode (`viewer=True`, no headless).
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import inspect
import json
import logging
import sys
import types
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

logger = logging.getLogger("sable.scraper")

from engine.config import BROWSER_SCRAPER_DATA_DIR

BASE_DIR = Path(__file__).resolve().parent.parent
SETTINGS_PATH = BASE_DIR / "system/scraper_settings.json"
ENGINES_DIR = BASE_DIR / "engine" / "scraper_engines"

# Registry of available scraper engines
ENGINE_REGISTRY: dict[str, dict[str, str]] = {
    "qwen": {
        "label": "Qwen",
        "path": str(ENGINES_DIR / "qwen" / "qwen_engine.py"),
    },
    "deepseek": {
        "label": "DeepSeek",
        "path": str(ENGINES_DIR / "deepseek" / "deepseek_engine.py"),
    },
}

DEFAULT_ENGINE_TYPE = "qwen"

DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": False,
    "engine_type": DEFAULT_ENGINE_TYPE,
    "port": 9333,
    "headless": False,
    "show_thoughts": True,
}



def _resolve_engine_path(engine_type: str) -> str:
    """Resolve engine_type to its file path from the registry."""
    entry = ENGINE_REGISTRY.get(engine_type)
    if entry:
        return entry["path"]
    return ENGINE_REGISTRY[DEFAULT_ENGINE_TYPE]["path"]


def _load_settings() -> dict[str, Any]:
    settings = dict(DEFAULT_SETTINGS)
    if SETTINGS_PATH.exists():
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                settings.update(stored)
        except Exception as exc:
            logger.warning("Could not read system/scraper_settings.json: %s", exc)

    # Migrate legacy engine_path to engine_type
    if "engine_path" in settings and "engine_type" not in settings:
        old_path = settings.pop("engine_path", "")
        for etype, entry in ENGINE_REGISTRY.items():
            if entry["path"] == old_path:
                settings["engine_type"] = etype
                break

    # Hard requirement from Sifat: scraper browser must be headed.
    settings["headless"] = False
    return settings


def get_settings() -> dict[str, Any]:
    settings = _load_settings()
    engine_type = settings.get("engine_type", DEFAULT_ENGINE_TYPE)
    engine_path = _resolve_engine_path(engine_type)
    settings["engine_path"] = engine_path
    settings["engine_exists"] = Path(engine_path).exists()
    settings["engine_label"] = ENGINE_REGISTRY.get(engine_type, {}).get("label", engine_type)
    return settings


def list_engines() -> list[dict[str, str]]:
    """Return available scraper engines for the UI."""
    return [
        {"id": etype, "label": entry["label"], "path": entry["path"]}
        for etype, entry in ENGINE_REGISTRY.items()
    ]


def save_settings(settings: dict[str, Any]) -> None:
    clean = {key: settings.get(key, DEFAULT_SETTINGS[key]) for key in DEFAULT_SETTINGS}
    clean["headless"] = False
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(clean, f, indent=2, ensure_ascii=False)


def update_settings(payload: dict[str, Any]) -> dict[str, Any]:
    settings = _load_settings()

    if "enabled" in payload:
        settings["enabled"] = bool(payload.get("enabled"))
    if "engine_type" in payload:
        engine_type = str(payload.get("engine_type") or "").strip()
        if engine_type in ENGINE_REGISTRY:
            settings["engine_type"] = engine_type
    if "port" in payload:
        try:
            settings["port"] = int(payload.get("port") or settings["port"])
        except (TypeError, ValueError):
            pass
    if "show_thoughts" in payload:
        settings["show_thoughts"] = bool(payload.get("show_thoughts"))

    # Never allow headless from the API/UI.
    settings["headless"] = False
    save_settings(settings)
    return get_settings()


def _load_py_module(module_name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _accepts_arg(func: Any, name: str) -> bool:
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return False

    for param in sig.parameters.values():
        if param.name == name:
            return True
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            return True
    return False


class ScraperEngine:
    """Singleton-ish adapter around a GhostChat-style browser scraper."""

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

        # When not killing the browser, keep self.engine intact so the next
        # toggle-on reuses the exact same connection. No new Chrome window.
        if not kill_browser:
            return

        # Full teardown: null out engine and reset chat tracking.
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

            # Sable runs the scraper as a normal headed browser, not inside
            # Obsidian's webview partition system.
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

        # Scraper gets its own persistent profile (separate from ChatService).
        try:
            engine.user_data_dir = str(BROWSER_SCRAPER_DATA_DIR)
        except Exception:
            pass

        try:
            # Launch the headed browser FIRST, then connect via CDP.
            launch = getattr(engine, "launch_chrome", None)
            if launch is not None:
                await launch()
            await engine.connect()
        except SystemExit as exc:
            raise RuntimeError(f"Browser engine exited during startup: {exc}") from exc

        self.engine = engine
        self.loaded_path = engine_path
        return engine

    async def _is_browser_alive(self, engine: Any) -> bool:
        """Quick CDP liveness probe — returns False if browser was closed externally."""
        port = getattr(engine, "port", None)
        if port is None:
            return True  # no port to check, assume alive
        import socket
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1.0)
                return s.connect_ex(("127.0.0.1", int(port))) == 0
        except Exception:
            return False

    async def _probe_existing_session(self, settings: dict[str, Any]) -> bool:
        """Check if a headed browser is already running on the configured CDP port.

        Returns True if we successfully reconnected to it, False otherwise.
        This avoids spawning a duplicate Chrome window after toggle off/on.
        """
        import socket
        import urllib.request

        port = settings.get("port", DEFAULT_SETTINGS["port"])
        user_data_dir = str(BROWSER_SCRAPER_DATA_DIR)

        # Quick TCP check first
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1.0)
                if s.connect_ex(("127.0.0.1", port)) != 0:
                    return False
        except Exception:
            return False

        # Verify it's our headed session with matching profile
        try:
            url = f"http://127.0.0.1:{port}/json/version"
            req = urllib.request.Request(url, headers={"User-Agent": "GhostChat"})
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                data = json.loads(resp.read().decode())
            remote_profile = data.get("userDataDir", "") or data.get("profile-path", "")
            import os
            if os.path.realpath(remote_profile) != os.path.realpath(user_data_dir):
                return False
            cmd_line = data.get("BrowserCommandLine", "")
            if "--headless" in cmd_line:
                return False
        except Exception:
            return False

        # Existing headed session found — reconnect without launching new Chrome
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
        """Pre-launch the browser when scraper mode is enabled.

        Called from the API so the headed browser opens immediately
        instead of waiting for the first message.
        Reuses an existing headed session if one is already running.
        """
        settings = _load_settings()
        if not settings.get("enabled"):
            return {"status": "skipped", "message": "Scraper is disabled"}

        try:
            async with self._lock:
                # Skip re-launch if engine is already alive with same config
                engine_type = settings.get("engine_type", DEFAULT_ENGINE_TYPE)
                engine_path = _resolve_engine_path(engine_type)
                if self.engine is not None and self.loaded_path == engine_path:
                    return {"status": "ok", "message": "Browser already running"}
                # Try reconnecting to existing headed session before launching new one
                if await self._probe_existing_session(settings):
                    return {"status": "ok", "message": "Reconnected to existing browser"}
                await self._ensure_engine(settings)
            return {"status": "ok", "message": "Browser launched and connected"}
        except Exception as exc:
            logger.exception("Browser prelaunch failed")
            return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}

    async def switch_model(self, model_type: str) -> dict[str, Any]:
        """Switch the active model type on the browser engine (e.g. DeepSeek Instant/Expert/Vision).

        Opens a new browser chat and clicks the corresponding model button.
        """
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
                    # Reset chat tracking so the next message starts fresh
                    self.active_chat_id = None
                    return {"status": "ok", "model_type": model_type}
                return {"status": "error", "message": f"Could not switch to {model_type}"}
        except Exception as exc:
            logger.exception("Model switch failed")
            return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}

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
        import asyncio
        import os
        import signal

        engine = self.engine
        killed_pid: int | None = None

        if engine is not None:
            # Phase 1: try graceful cleanup via engine (flushes LevelDB properly)
            cleanup = getattr(engine, "cleanup", None)
            if cleanup is not None:
                try:
                    result = cleanup()
                    if inspect.isawaitable(result):
                        await asyncio.wait_for(result, timeout=5.0)
                except Exception:
                    pass

            # Phase 2: if the process is still alive, SIGTERM the group
            chrome_proc = getattr(engine, "chrome_process", None)
            if chrome_proc is not None:
                try:
                    pid = chrome_proc.pid
                    pgid = os.getpgid(pid)
                    # Check if still running
                    os.kill(pid, 0)
                    os.killpg(pgid, signal.SIGTERM)
                    killed_pid = pid
                    # Give it 3s to flush and exit
                    await asyncio.sleep(3.0)
                    # Phase 3: SIGKILL only if STILL alive
                    try:
                        os.kill(pid, 0)  # raises if dead
                        os.killpg(pgid, signal.SIGKILL)
                    except (ProcessLookupError, OSError):
                        pass  # already dead, good
                except (ProcessLookupError, PermissionError, OSError):
                    pass  # process already gone

        await self.stop(kill_browser=True)
        return {"status": "ok", "killed_pid": killed_pid}

    async def _interrupt_generation(self, engine: Any) -> None:
        """Click the engine's on-page stop button so generation actually halts.

        Called when the client aborts the stream — without this the browser
        tab happily keeps generating for nobody, burning tokens in the
        background until it finishes on its own.
        """
        stop = getattr(engine, "stop_generation", None)
        if stop is None:
            return
        try:
            if await stop():
                logger.info("Browser generation stopped via on-page stop button")
        except Exception as exc:
            logger.warning("Could not click browser stop button: %s", exc)

    async def _stream_get_response(
        self,
        engine: Any,
        response_kwargs: dict[str, Any],
        state: dict[str, Any],
    ) -> AsyncGenerator[dict[str, Any], None]:
        '''Yield live thought + answer deltas while the browser engine captures text.'''
        if not _accepts_arg(engine.get_response, 'live_display'):
            answer = await engine.get_response(**response_kwargs)
            state['answer'] = str(answer or '')
            state['streamed'] = False
            return

        answer_queue: asyncio.Queue[str] = asyncio.Queue()
        thought_queue: asyncio.Queue[str] = asyncio.Queue()
        last_answer = ''
        last_thought = ''

        def live_display(full_text: str) -> None:
            nonlocal last_answer
            full = str(full_text or '')
            if not full or full == last_answer:
                return
            if full.startswith(last_answer):
                delta = full[len(last_answer):]
            else:
                delta = full
            last_answer = full
            if delta:
                answer_queue.put_nowait(delta)

        async def thoughts_callback(full_text: str) -> None:
            nonlocal last_thought
            full = str(full_text or '')
            if not full or full == last_thought:
                return
            if full.startswith(last_thought):
                delta = full[len(last_thought):]
            else:
                delta = full
            last_thought = full
            if delta:
                thought_queue.put_nowait(delta)

        kwargs = dict(response_kwargs)
        kwargs['live_display'] = live_display
        if _accepts_arg(engine.get_response, 'thoughts_callback'):
            kwargs['thoughts_callback'] = thoughts_callback
        task = asyncio.create_task(engine.get_response(**kwargs))

        while not task.done():
            yielded = False
            # Drain thoughts first — they should stream before the answer
            while not thought_queue.empty():
                delta = thought_queue.get_nowait()
                state['streamed_thoughts'] = True
                yield {'type': 'thinking', 'text': delta}
                yielded = True
            # Then answer deltas
            try:
                delta = await asyncio.wait_for(answer_queue.get(), timeout=0.15)
                state['streamed'] = True
                yield {'type': 'answer', 'text': delta}
                yielded = True
            except asyncio.TimeoutError:
                pass
            if not yielded:
                continue

        # Drain remaining thoughts
        while not thought_queue.empty():
            delta = thought_queue.get_nowait()
            state['streamed_thoughts'] = True
            yield {'type': 'thinking', 'text': delta}

        # Drain remaining answer
        while not answer_queue.empty():
            delta = answer_queue.get_nowait()
            state['streamed'] = True
            yield {'type': 'answer', 'text': delta}

        answer = await task
        answer_text = str(answer or '')
        state['answer'] = answer_text

        if answer_text:
            if answer_text.startswith(last_answer):
                tail = answer_text[len(last_answer):]
            else:
                tail = '' if state.get('streamed') else answer_text
            if tail:
                state['streamed'] = True
                yield {'type': 'answer', 'text': tail}


    async def stream_events(
        self,
        message: str,
        chat_id: str | None = None,
        parent_id: str | None = None,
        files: list[dict[str, Any]] | None = None,
        model: str | None = None,
        thinking_mode: str | None = None,
        chat_url: str | None = None,
        raw: bool = False,
    ) -> AsyncGenerator[dict[str, Any], None]:
        settings = _load_settings()
        if not settings.get("enabled"):
            yield {"type": "error", "message": "Browser scraper is disabled"}
            return

        engine: Any = None
        async with self._lock:
            try:
                yield {"type": "status", "message": "browser_scraper_starting"}
                engine = await self._ensure_engine(settings)
                yield {"type": "status", "message": "browser_scraper_connected"}

                # Quick liveness check — if the browser was closed externally,
                # _ensure_engine still returns the stale object. Probe CDP and
                # restart if dead so we don't fail silently on send_msg.
                if not await self._is_browser_alive(engine):
                    logger.warning("Browser process gone, restarting engine")
                    self.engine = None
                    self.loaded_path = None
                    engine = await self._ensure_engine(settings)
                    yield {"type": "status", "message": "browser_scraper_reconnected"}

                # DeepSeek: the requested model id rides along with every chat
                # request (and reflects whatever the UI has selected, including
                # a choice restored from localStorage on page load). Sync it onto
                # the engine BEFORE the new-chat reload below so the post-reload
                # re-application clicks the correct model button instead of
                # leaving DeepSeek snapped back to Instant.
                if model in ("default", "expert", "vision") and hasattr(engine, "current_model_type"):
                    engine.current_model_type = model

                if chat_id and chat_id != self.active_chat_id:
                    # If the engine just switched models it already opened a
                    # fresh chat (with the right model clicked) — adopt the new
                    # chat_id instead of opening a second, redundant chat.
                    if getattr(engine, "has_fresh_chat", False):
                        engine.has_fresh_chat = False
                    elif chat_url:
                        # Resume an existing scraper conversation by navigating
                        # to its stored URL instead of starting a fresh chat.
                        page = getattr(engine, "page", None)
                        if page is not None:
                            try:
                                current = page.url
                                if current != chat_url:
                                    await page.goto(chat_url, wait_until="domcontentloaded", timeout=15000)
                                    await asyncio.sleep(2)
                                    yield {"type": "status", "message": "browser_resumed_chat"}
                            except Exception as exc:
                                yield {
                                    "type": "status",
                                    "message": f"browser_resume_failed: {exc}",
                                }
                    else:
                        new_chat = getattr(engine, "new_chat", None)
                        if new_chat is not None:
                            try:
                                await new_chat()
                            except Exception as exc:
                                yield {
                                    "type": "status",
                                    "message": f"browser_new_chat_failed: {exc}",
                                }
                    self.active_chat_id = chat_id

                initial_count = 0
                get_response_count = getattr(engine, "get_response_count", None)
                if get_response_count is not None:
                    try:
                        initial_count = int(await get_response_count())
                    except Exception:
                        initial_count = 0

                if files:
                    upload_file = getattr(engine, "upload_file", None)
                    if upload_file is not None:
                        for file_entry in files:
                            path = file_entry.get("path") or file_entry.get("local_path")
                            if not path:
                                continue
                            try:
                                await upload_file(path, has_msg=False)
                                yield {
                                    "type": "status",
                                    "message": f"browser_file_attached:{Path(path).name}",
                                }
                            except Exception as exc:
                                yield {
                                    "type": "status",
                                    "message": f"browser_file_attach_failed:{exc}",
                                }

                # Apply the requested thinking mode (DeepThink on/off) right
                # before sending so the reply is generated with the correct
                # reasoning setting. Only DeepSeek implements this; other
                # engines simply ignore it.
                if thinking_mode in ("deepthink", "fast"):
                    set_thinking = getattr(engine, "set_thinking_mode", None)
                    if set_thinking is not None:
                        try:
                            await set_thinking(thinking_mode)
                        except Exception as exc:
                            yield {
                                "type": "status",
                                "message": f"browser_thinking_mode_failed: {exc}",
                            }

                send_kwargs: dict[str, Any] = {}
                if _accepts_arg(engine.send_msg, 'raw'):
                    send_kwargs['raw'] = raw
                sent = await engine.send_msg(message, **send_kwargs)
                if not sent:
                    yield {
                        "type": "error",
                        "message": "Browser scraper could not send the message",
                    }
                    return

                yield {"type": "status", "message": "waiting_for_browser_response"}

                response_kwargs: dict[str, Any] = {}
                if _accepts_arg(engine.get_response, "initial_count"):
                    response_kwargs["initial_count"] = initial_count

                scraper_state: dict[str, Any] = {}
                async for event in self._stream_get_response(engine, response_kwargs, scraper_state):
                    yield event
                answer = str(scraper_state.get('answer', ''))
                streamed_answer = bool(scraper_state.get('streamed', False))

                answer_text = str(answer or "")
                if not streamed_answer:
                    if answer_text:
                        yield {"type": "answer", "text": answer_text}
                    else:
                        yield {
                            "type": "status",
                            "message": "browser_scraper_empty_response",
                        }

                new_parent = f"browser-{uuid.uuid4().hex}"
                # Capture the browser URL after response completes — this is
                # the conversation URL that lets us resume this chat later.
                chat_url = None
                try:
                    page = getattr(engine, "page", None)
                    if page is not None:
                        chat_url = page.url
                except Exception:
                    pass
                yield {
                    "type": "done",
                    "chat_id": chat_id,
                    "parent_id": new_parent,
                    "chat_url": chat_url,
                }
            except (asyncio.CancelledError, GeneratorExit):
                # The client hit stop (or the stream was cut mid-generation).
                # Abort the fetch alone doesn't reach the webpage — click the
                # real stop button so DeepSeek/Qwen actually stops generating.
                if engine is not None:
                    await self._interrupt_generation(engine)
                raise
            except SystemExit as exc:
                yield {
                    "type": "error",
                    "message": f"Browser engine exited unexpectedly: {exc}",
                }
            except Exception as exc:
                logger.exception("Browser scraper failed")
                yield {
                    "type": "error",
                    "message": f"{type(exc).__name__}: {exc}",
                }

    async def chat(
        self,
        message: str,
        chat_id: str | None = None,
        parent_id: str | None = None,
        files: list[dict[str, Any]] | None = None,
        model: str | None = None,
        thinking_mode: str | None = None,
        chat_url: str | None = None,
        raw: bool = False,
    ) -> dict[str, Any]:
        thinking_parts: list[str] = []
        answer_parts: list[str] = []
        tool_events: list[dict[str, Any]] = []
        final_chat_id = chat_id
        final_parent_id = parent_id
        final_chat_url: str | None = None
        error: str | None = None

        async for event in self.stream_events(
            message=message,
            chat_id=chat_id,
            parent_id=parent_id,
            files=files,
            model=model,
            thinking_mode=thinking_mode,
            chat_url=chat_url,
            raw=raw,
        ):
            event_type = event.get("type")
            if event_type == "thinking":
                thinking_parts.append(str(event.get("text", "")))
            elif event_type == "answer":
                answer_parts.append(str(event.get("text", "")))
            elif event_type in ("tool_call", "tool_result"):
                tool_events.append(event)
            elif event_type == "done":
                final_chat_id = event.get("chat_id") or final_chat_id
                final_parent_id = event.get("parent_id") or final_parent_id
                final_chat_url = event.get("chat_url") or final_chat_url
            elif event_type == "error":
                error = str(event.get("message", "Unknown scraper error"))

        return {
            "chat_id": final_chat_id,
            "parent_id": final_parent_id,
            "chat_url": final_chat_url,
            "thinking": "".join(thinking_parts),
            "answer": "".join(answer_parts),
            "tool_events": tool_events,
            "error": error,
        }


scraper = ScraperEngine()
