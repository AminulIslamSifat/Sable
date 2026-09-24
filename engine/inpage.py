"""Ghost Tab Engine — Qwen requests executed inside a live browser tab.

Primary Qwen path: every API call is an in-page fetch() on a parked "ghost"
tab, so Alibaba's Baxia SDK signs the request at send-time (token age: zero,
genuine TLS, live cookies). The raw curl_cffi path in engine/chat.py becomes
the fallback, triggered whenever this module raises GhostError.

Lifecycle (Sifat spec, 2026-09-21):
  1. ghost already healthy             -> reuse
  2. same-process BrowserManager live  -> adopt its context (shared)
  3. marker file (CDP port) from a     -> try connect_over_cdp
     ghost launched by an earlier         fail -> kill + relaunch
     process
  4. profile locked by a live foreign  -> Sable server running? bail to API.
     chromium                             otherwise kill, clean lock, relaunch

Payload routing (the 2026-09-21 size map):
  len(message) <= RAW_TEXT_LIMIT  -> raw in-page fetch
  len(message) >  RAW_TEXT_LIMIT  -> STS -> OSS .txt upload (in-page STS),
                                     short message + files=[file_obj]
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import AsyncIterator

from engine.config import NEW_CHAT_URL, URL, get_browser_data_dir, get_model_config
from engine.payloads import build_body

logger = logging.getLogger("sable.inpage")

GHOST_HOME = "https://chat.qwen.ai"
STS_URL = "https://chat.qwen.ai/api/v2/files/getstsToken"
RAW_TEXT_LIMIT = 100_000  # safe margin under the ~120k RGV587 punish wall

# Idle timeout: if no event arrives within this window — while connecting or
# mid-stream — the in-page fetch is aborted and a fresh connection is opened.
# Only retried while NOTHING has been emitted yet (a stall after partial
# content can't be replayed without duplicating the answer).
_GHOST_IDLE_SECS = 30.0
_GHOST_MAX_ATTEMPTS = 3


class GhostError(Exception):
    """Ghost path failed — caller must fall back to the raw curl_cffi API."""


def _server_running() -> bool:
    """True if Sable's own server process (which owns its browser) is up."""
    pid_file = Path(__file__).resolve().parent.parent / ".sable_server.pid"
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _lock_pid(user_data_dir: str) -> int | None:
    lock = Path(user_data_dir) / "SingletonLock"
    if not lock.is_symlink():
        return None
    try:
        return int(lock.readlink().name.rsplit("-", 1)[-1])
    except Exception:
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# In-page JS snippets — every header that matters is generated INSIDE the page
# (fresh x-request-id / timezone per call; proven recipe from test v5-v9).
# ---------------------------------------------------------------------------

JS_CREATE_CHAT = r"""
async ({url, model}) => {
  const t0 = performance.now();
  try {
    const h = {
      'accept': 'application/json, text/plain, */*',
      'content-type': 'application/json',
      'source': 'web', 'version': '0.2.91', 'bx-v': '2.5.37',
      'x-request-id': crypto.randomUUID(),
      'timezone': new Date().toString(),
    };
    const body = {chatId: '', models: [model], project_id: '',
                  timestamp: Date.now(), chat_type: 't2t', chat_mode: 'normal'};
    const res = await fetch(url, {method: 'POST', credentials: 'include',
                                  headers: h, body: JSON.stringify(body)});
    return {status: res.status, ms: Math.round(performance.now() - t0),
            text: await res.text()};
  } catch (e) {
    return {status: -1, ms: Math.round(performance.now() - t0), err: String(e), text: ''};
  }
}
"""

JS_STREAM_COMPLETIONS = r"""
async ({url, body, rid, idleMs}) => {
  try {
    const h = {
      'accept': 'application/json',
      'content-type': 'application/json',
      'source': 'web', 'version': '0.2.91', 'bx-v': '2.5.37',
      'x-accel-buffering': 'no',
      'x-request-id': crypto.randomUUID(),
      'timezone': new Date().toString(),
    };
    const ctrl = new AbortController();
    let idleTimer = setTimeout(() => ctrl.abort(), idleMs);
    const res = await fetch(url, {method: 'POST', credentials: 'include',
                                  headers: h, body: JSON.stringify(body),
                                  signal: ctrl.signal});
    await window.__ghostEvent({rid, t: 'status', status: res.status});
    if (!res.body) { await window.__ghostEvent({rid, t: 'done'}); return 'nobody'; }
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    try {
      while (true) {
        clearTimeout(idleTimer);
        idleTimer = setTimeout(() => ctrl.abort(), idleMs);
        const {done, value} = await reader.read();
        if (done) break;
        await window.__ghostEvent({rid, t: 'chunk', data: dec.decode(value, {stream: true})});
      }
    } finally {
      clearTimeout(idleTimer);
    }
    await window.__ghostEvent({rid, t: 'done'});
    return 'ok';
  } catch (e) {
    await window.__ghostEvent({rid, t: 'error', err: String(e)});
    return 'err';
  }
}
"""

JS_STS_TOKEN = r"""
async ({url, payload}) => {
  try {
    const h = {
      'accept': 'application/json, text/plain, */*',
      'content-type': 'application/json',
      'source': 'web', 'version': '0.2.91', 'bx-v': '2.5.37',
      'x-request-id': crypto.randomUUID(),
      'timezone': new Date().toString(),
    };
    const res = await fetch(url, {method: 'POST', credentials: 'include',
                                  headers: h, body: JSON.stringify(payload)});
    return {status: res.status, text: await res.text()};
  } catch (e) {
    return {status: -1, text: '', err: String(e)};
  }
}
"""


class _Pipe:
    """Bridge for expose_function events -> asyncio queue."""

    def __init__(self) -> None:
        self.q: asyncio.Queue = asyncio.Queue()


class GhostEngine:
    """Owns (or adopts) the browser + ghost tab and runs in-page Qwen calls."""

    _instance: "GhostEngine | None" = None

    @classmethod
    def instance(cls) -> "GhostEngine":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self, headless: bool = True, user_data_dir: str | None = None) -> None:
        self.user_data_dir = user_data_dir or str(get_browser_data_dir())
        self.headless = headless
        self._pw = None
        self._context = None
        self._cdp_browser = None
        self._shared_bm = None  # engine.chat's BrowserManager, if adopted
        self._ghost = None
        self._exposed_pages: set = set()
        self._stream_lock = asyncio.Lock()
        self._pipe = _Pipe()
        # Request id for the in-flight stream. Events tagged with a stale rid
        # (from a connection we already dropped) are ignored so a retry can't
        # be polluted by the abandoned fetch's trailing chunks.
        self._current_rid: str | None = None

    # -- lifecycle ----------------------------------------------------------

    async def ensure(self):
        """Return a healthy ghost tab, going through the full lifecycle if needed."""
        if self._ghost is not None:
            if not self._ghost.is_closed():
                try:
                    await asyncio.wait_for(
                        self._ghost.evaluate("() => location.origin"), 5)
                    return self._ghost
                except Exception:
                    pass
            self._ghost = None

        if self._context is not None:
            await self._spawn_ghost()
            return self._ghost

        # 0) same-process BrowserManager (engine.chat's bm) already running?
        try:
            from engine.chat import bm as chat_bm
            if chat_bm.is_running and chat_bm.context:
                self._context = chat_bm.context
                self._shared_bm = chat_bm
                logger.debug("adopting in-process BrowserManager context")
                await self._spawn_ghost()
                return self._ghost
        except Exception:
            pass

        # 1) adopt a browser we launched in a previous process (marker + CDP)
        if await self._adopt_existing():
            await self._spawn_ghost()
            return self._ghost

        # 2) foreign lock? Sable server -> bail; orphan chromium -> kill it
        self._check_foreign_lock()
        await self._hard_cleanup()

        # 3) fresh launch
        await self._launch()
        await self._spawn_ghost()
        return self._ghost

    async def _launch(self) -> None:
        from playwright.async_api import async_playwright
        from engine.platform_paths import resolve_browser_for_profile, extra_browser_args

        self._pw = await async_playwright().start()
        profile = Path(self.user_data_dir).name
        exe = resolve_browser_for_profile(profile)

        # free port for the CDP endpoint so future processes can adopt us
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()

        args = [
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--disable-gpu",
            f"--remote-debugging-port={port}",
        ] + extra_browser_args(exe)
        kwargs = dict(user_data_dir=self.user_data_dir, headless=self.headless, args=args)
        if exe:
            kwargs["executable_path"] = exe

        logger.debug(f"launching headless Chromium on {profile} (cdp={port})...")
        self._context = await self._pw.chromium.launch_persistent_context(**kwargs)

        await asyncio.sleep(0.5)
        pid = _lock_pid(self.user_data_dir)
        marker = Path(self.user_data_dir) / ".ghost_engine.json"
        marker.write_text(json.dumps({"port": port, "pid": pid, "ts": time.time()}))

    async def _adopt_existing(self) -> bool:
        marker = Path(self.user_data_dir) / ".ghost_engine.json"
        if not marker.exists():
            return False
        try:
            m = json.loads(marker.read_text())
            port = int(m["port"])
        except Exception:
            return False

        pid = _lock_pid(self.user_data_dir)
        if pid is None or not _pid_alive(pid):
            marker.unlink(missing_ok=True)
            return False

        from playwright.async_api import async_playwright
        try:
            self._pw = await async_playwright().start()
            self._cdp_browser = await self._pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{port}", timeout=8000)
            ctxs = self._cdp_browser.contexts
            if not ctxs:
                raise RuntimeError("no contexts over CDP")
            self._context = ctxs[0]
            logger.debug(f"adopted running browser over CDP (port {port})")
            return True
        except Exception as e:
            logger.debug(f"CDP connect failed ({e}) — killing + relaunching")
            await self._hard_cleanup()
            return False

    def _check_foreign_lock(self) -> None:
        lock = Path(self.user_data_dir) / "SingletonLock"
        if not (lock.is_symlink() or lock.exists()):
            return
        pid = _lock_pid(self.user_data_dir)
        if pid is None or not _pid_alive(pid):
            try:
                lock.unlink(missing_ok=True)
            except OSError:
                pass
            return
        if _server_running():
            raise GhostError(
                "profile locked by the running Sable server's browser — "
                "ghost can't take it over; use the raw API path")
        logger.debug(f"profile locked by orphan chromium (pid {pid}) — killing")

    async def _hard_cleanup(self) -> None:
        pid = _lock_pid(self.user_data_dir)
        if pid is not None and _pid_alive(pid):
            import signal
            try:
                os.kill(pid, signal.SIGTERM)
                for _ in range(10):
                    await asyncio.sleep(0.3)
                    if not _pid_alive(pid):
                        break
                else:
                    os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        for p in (Path(self.user_data_dir) / "SingletonLock",
                  Path(self.user_data_dir) / ".ghost_engine.json"):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
        self._pw = None
        self._context = None
        self._cdp_browser = None

    async def _spawn_ghost(self) -> None:
        page = await self._context.new_page()
        await page.goto(GHOST_HOME, wait_until="domcontentloaded", timeout=20000)
        healthy = False
        for _ in range(16):  # up to 8s: auth token + WAF cookies bake
            try:
                tok = await page.evaluate("() => !!localStorage.getItem('token')")
            except Exception:
                tok = False
            names = {c["name"] for c in await self._context.cookies()}
            if tok and {"acw_tc", "isg"} <= names:
                healthy = True
                break
            await page.wait_for_timeout(500)
        if not healthy:
            try:
                await page.close()
            except Exception:
                pass
            raise GhostError(
                "ghost tab not healthy (auth token / WAF cookies missing — stale profile?)")
        if page not in self._exposed_pages:
            await page.expose_function("__ghostEvent", self._on_event)
            self._exposed_pages.add(page)
        self._ghost = page
        logger.debug("ghost tab ready (token + WAF cookies baked)")

    def _on_event(self, ev: dict) -> None:
        # Drop events from a connection we've already abandoned (rid mismatch).
        if ev.get("rid") != self._current_rid:
            return
        self._pipe.q.put_nowait(ev)

    async def shutdown(self) -> None:
        """Full teardown. If the context is shared with engine.chat's bm, only
        the ghost page dies — the shared browser stays for the curl path."""
        try:
            if self._ghost is not None:
                await self._ghost.close()
        except Exception:
            pass
        self._ghost = None
        if self._shared_bm is None:
            try:
                if self._context is not None:
                    await self._context.close()
            except Exception:
                pass
            if self._pw is not None:
                try:
                    await self._pw.stop()
                except Exception:
                    pass
            try:
                (Path(self.user_data_dir) / ".ghost_engine.json").unlink(missing_ok=True)
            except OSError:
                pass
        self._context = None
        self._cdp_browser = None
        self._pw = None
        self._shared_bm = None
        self._exposed_pages.clear()

    # -- API calls ----------------------------------------------------------

    async def create_chat(self, model_id: str) -> str:
        ghost = await self.ensure()
        try:
            r = await asyncio.wait_for(
                ghost.evaluate(JS_CREATE_CHAT, {"url": NEW_CHAT_URL, "model": model_id}), 30)
        except asyncio.TimeoutError:
            raise GhostError("chats/new hung >30s (tarpit)")
        if r.get("status") != 200:
            raise GhostError(f"chats/new HTTP {r.get('status')}")
        text = r.get("text", "")
        if text.lstrip().startswith("<"):
            raise GhostError("chats/new returned captcha HTML")
        try:
            d = json.loads(text)
        except Exception:
            raise GhostError(f"chats/new non-JSON: {text[:120]}")
        if not d.get("success"):
            raise GhostError(f"chats/new refused: {text[:150]}")
        cid = d.get("data", {}).get("id")
        if not cid:
            raise GhostError("chats/new returned no id")
        return cid

    async def stream(
        self, url: str, body: dict,
        idle_secs: float = _GHOST_IDLE_SECS,
        max_attempts: int = _GHOST_MAX_ATTEMPTS,
    ) -> AsyncIterator[dict]:
        """Yield events: {t:'status'|'chunk'|'error'|'done', ...} from one in-page fetch.

        Idle gate + reconnect: if no event arrives within `idle_secs` — while
        connecting or mid-stream — the in-page fetch is aborted and a FRESH
        connection is opened (up to `max_attempts`). A stall after any chunk
        has been yielded is fatal (can't replay partial content), so it raises
        immediately for the caller's curl fallback.
        """
        async with self._stream_lock:
            for attempt in range(max_attempts):
                ghost = await self.ensure()
                rid = uuid.uuid4().hex
                self._current_rid = rid
                self._pipe = _Pipe()
                # In-page idle abort is set ABOVE the Python deadline (+5s) so
                # Python's wait_for always fires first — a stall is then handled
                # as a reconnect, not as an in-page 'error' event. The JS timer
                # still runs as the actual network cleanup for the dropped fetch.
                task = asyncio.create_task(
                    ghost.evaluate(JS_STREAM_COMPLETIONS, {
                        "url": url, "body": body, "rid": rid,
                        "idleMs": int((idle_secs + 5) * 1000),
                    }))
                yielded_chunk = False
                stalled = False
                try:
                    while True:
                        try:
                            ev = await asyncio.wait_for(
                                self._pipe.q.get(), timeout=idle_secs)
                        except asyncio.TimeoutError:
                            stalled = True
                            break
                        yield ev
                        if ev.get("t") == "chunk":
                            yielded_chunk = True
                        if ev.get("t") in ("done", "error"):
                            try:
                                await asyncio.wait_for(task, timeout=10)
                            except Exception:
                                pass
                            return
                finally:
                    # Drop the connection: cancel the evaluate; the in-page
                    # AbortController (fired at the same idle deadline) kills
                    # the underlying fetch so no further bytes are read.
                    if not task.done():
                        task.cancel()
                    self._current_rid = None
                if not stalled:
                    return
                if yielded_chunk or attempt == max_attempts - 1:
                    raise GhostError(
                        f"stream stall >{idle_secs:.0f}s "
                        f"(attempt {attempt + 1}/{max_attempts}, "
                        f"chunk_seen={yielded_chunk})")
                logger.debug(f"stream stall — reconnecting (attempt {attempt + 2}/{max_attempts})")
                # Surface the reconnect to the caller's status stream.
                yield {"t": "reconnect", "attempt": attempt + 2, "max": max_attempts}

    async def upload_file_inpage(self, path: str) -> dict:
        """STS via in-page fetch (live cookies, never stale) + oss2 PUT + file_obj."""
        ghost = await self.ensure()
        p = Path(path)
        filesize = p.stat().st_size
        filename = p.name
        from connectors.common.media import detect_mime
        mime = detect_mime(p)

        payload = {"filename": filename, "filesize": str(filesize), "filetype": "file"}
        try:
            r = await asyncio.wait_for(
                ghost.evaluate(JS_STS_TOKEN, {"url": STS_URL, "payload": payload}), 30)
        except asyncio.TimeoutError:
            raise GhostError("getstsToken hung >30s")
        if r.get("status") != 200:
            raise GhostError(f"getstsToken HTTP {r.get('status')}: {r.get('text', '')[:120]}")
        try:
            d = json.loads(r["text"])
        except Exception:
            raise GhostError(f"getstsToken non-JSON: {r.get('text', '')[:120]}")
        if not d.get("success"):
            raise GhostError(f"getstsToken rejected: {r.get('text', '')[:150]}")
        sts = d["data"]

        def _put() -> int:
            import oss2
            auth = oss2.StsAuth(sts["access_key_id"], sts["access_key_secret"],
                                sts["security_token"])
            bucket = oss2.Bucket(auth, f"https://{sts['endpoint']}", sts["bucketname"])
            with open(path, "rb") as f:
                return bucket.put_object(sts["file_path"], f).status

        status = await asyncio.to_thread(_put)
        if status not in (200, 204):
            raise GhostError(f"OSS PUT failed: HTTP {status}")

        now_ms = int(time.time() * 1000)
        file_id = sts["file_id"]
        return {
            "type": "file",
            "file": {
                "created_at": now_ms, "data": {}, "filename": filename, "hash": None,
                "id": file_id, "user_id": sts["file_path"].split("/")[0],
                "meta": {"name": filename, "size": filesize, "content_type": mime,
                         "parse_meta": {"parse_status": "success"}},
                "update_at": now_ms, "lastModified": now_ms, "name": filename,
                "webkitRelativePath": "", "size": filesize, "type": mime,
            },
            "id": file_id, "url": sts.get("file_url", ""), "name": filename,
            "collection_name": "", "progress": 0, "status": "uploaded",
            "greenNet": "success", "size": filesize, "error": "",
            "itemId": str(uuid.uuid4()), "file_type": mime,
            "showType": "file", "file_class": "document",
            "uploadTaskId": str(uuid.uuid4()),
        }


def _dump_payload(text: str) -> Path:
    d = Path("/home/sifat/sable_output/sessions")
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"ghost_payload_{int(time.time())}.txt"
    p.write_text(text, encoding="utf-8")
    return p


async def ghost_stream_chat(
    message: str,
    chat_id: str | None = None,
    parent_id: str | None = None,
    model: str | None = None,
    thinking_mode: str | None = None,
) -> tuple[str | None, str | None]:
    """Full ghost-path chat: create -> (file route if huge) -> in-page stream.

    Mirrors engine.chat.stream_chat's output format. Raises GhostError on any
    failure so the caller can fall back to the raw curl_cffi path.
    """
    try:
        from engine.chat import _log_raw_line, _log_stream_chunk
    except Exception:
        def _log_raw_line(line: str) -> None:
            pass
        def _log_stream_chunk(phase: str, content: str) -> None:
            pass

    eng = GhostEngine.instance()
    model_id = get_model_config(model)["id"]

    # Payload routing — the size wall is server-side, so route around it.
    files = None
    send_text = message
    if len(message) > RAW_TEXT_LIMIT:
        dump = _dump_payload(message)
        logger.debug(f"{len(message)} chars > {RAW_TEXT_LIMIT} — file route ({dump.name})")
        fobj = await eng.upload_file_inpage(str(dump))
        files = [fobj]
        send_text = (message[:300]
                     + "\n\n[...full text attached as a document — read the file.]")

    if not chat_id:
        chat_id = await eng.create_chat(model_id)

    body = build_body(send_text, chat_id, parent_id, files=files, model=model_id,
                      thinking_mode=thinking_mode)
    url = f"{URL}?chat_id={chat_id}"  # bare URL = 200 + empty stream forever

    logger.debug(f"in-page completions chat={chat_id[:8]}… model={model_id} chars={len(send_text)}")

    buf = ""
    answer: list[str] = []
    thinking: list[str] = []
    in_thinking = in_answer = False
    new_parent = parent_id
    chosen: str | None = None
    saw_data = False

    async for ev in eng.stream(url, body):
        t = ev.get("t")
        if t == "chunk":
            data = ev.get("data", "")
            if "RGV587" in data:
                raise GhostError("captcha punish (RGV587) — WAF flagged the payload")
            buf += data
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                _log_raw_line(line)
                if not line.startswith("data: "):
                    try:
                        err = json.loads(line)
                        if err.get("success") is False or "error" in err:
                            raise GhostError(f"stream error event: {line[:200]}")
                    except json.JSONDecodeError:
                        pass
                    continue
                try:
                    d = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                saw_data = True
                u = d.get("usage")
                if isinstance(u, dict) and u:
                    _log_stream_chunk("usage", json.dumps(u))
                created = d.get("response.created")
                if isinstance(created, dict):
                    rid = created.get("response_id")
                    if isinstance(rid, str) and (created.get("response_index") == "0" or chosen is None):
                        chosen = rid
                        new_parent = rid
                    continue
                choices = d.get("choices", [])
                if not choices:
                    if d.get("error"):
                        raise GhostError(f"Qwen error: {json.dumps(d)[:200]}")
                    continue
                rid = d.get("response_id")
                if isinstance(rid, str):
                    if chosen is None:
                        chosen = rid
                        new_parent = rid
                    elif rid != chosen:
                        continue
                delta = choices[0].get("delta", {})
                phase = delta.get("phase", "")
                content = delta.get("content", "")
                if phase in ("thinking", "thinking_summary"):
                    extra = delta.get("extra", {})
                    th = extra.get("summary_thought", {}).get("content", [])
                    text = "".join(th) if th else content
                    if text:
                        _log_stream_chunk(phase, text)
                        thinking.append(text)
                        if not in_thinking:
                            sys.stdout.write("\n🧠 [Thinking]\n")
                            in_thinking = True
                        sys.stdout.write(text)
                        sys.stdout.flush()
                elif phase == "answer" and content:
                    _log_stream_chunk("answer", content)
                    answer.append(content)
                    if not in_answer:
                        if in_thinking:
                            sys.stdout.write("\n\n💬 [Answer]\n")
                        in_answer = True
                    sys.stdout.write(content)
                    sys.stdout.flush()
        elif t == "error":
            raise GhostError(f"in-page fetch error: {ev.get('err')}")

    if not answer and not thinking:
        raise GhostError("empty stream (silent reject / tarpit)")

    return chat_id, new_parent


def _parse_sse(d: dict, state: dict) -> list[dict]:
    """Parse one SSE payload into service-protocol events (may be empty)."""
    out: list[dict] = []
    created = d.get("response.created")
    if isinstance(created, dict):
        rid = created.get("response_id")
        if isinstance(rid, str) and (created.get("response_index") == "0" or state["chosen"] is None):
            state["chosen"] = rid
            state["new_parent"] = rid
            out.append({"type": "response_id", "id": rid})
        return out

    u = d.get("usage")
    if isinstance(u, dict) and u:
        out.append({"type": "usage", "data": u})

    choices = d.get("choices", [])
    if not choices:
        if d.get("error"):
            raise GhostError(f"Qwen error: {json.dumps(d)[:200]}")
        return out

    rid = d.get("response_id")
    if isinstance(rid, str):
        if state["chosen"] is None:
            state["chosen"] = rid
            state["new_parent"] = rid
            out.append({"type": "response_id", "id": rid})
        elif rid != state["chosen"]:
            return out

    delta = choices[0].get("delta", {})
    phase = delta.get("phase", "")
    content = delta.get("content", "")
    if phase in ("thinking", "thinking_summary"):
        extra = delta.get("extra", {})
        th = extra.get("summary_thought", {}).get("content", [])
        text = "".join(th) if th else content
        if text:
            state["saw_content"] = True
            out.append({"type": "thinking", "text": text})
    elif phase == "answer" and content:
        state["saw_content"] = True
        out.append({"type": "answer", "text": content})

    tc = delta.get("tool_calls")
    if tc:
        state["saw_content"] = True
        out.append({"type": "tool_call", "data": tc})
    return out


async def ghost_stream_events(
    message: str,
    chat_id: str | None = None,
    parent_id: str | None = None,
    model: str | None = None,
    thinking_mode: str | None = None,
) -> AsyncIterator[dict]:
    """Server-protocol event generator (mirrors ChatService.stream_events).

    Fallback contract with stream_events:
      * A {"type": "status", "message": "qwen_transport_ghost"} is emitted
        first, then status/retry events as the in-page stream progresses.
      * If the ghost produces NOTHING parseable (create fails, status != 200,
        empty stream, tarpit), this raises GhostError — the caller falls back
        to the raw API path cleanly. Status events already yielded are purely
        informational and safe to precede a fallback.
      * Once content has been emitted, a failure becomes a terminal
        {"type": "error"} event instead (falling back then would duplicate
        a partial answer).
    """
    eng = GhostEngine.instance()
    model_id = get_model_config(model)["id"]
    from connectors.common.error_ctx import ErrorContext
    _ctx = ErrorContext("qwen")
    _ctx.model = model_id
    import os as _os
    _ctx.account = _os.path.basename(eng.user_data_dir.rstrip("/")) or None

    files = None
    send_text = message
    if len(message) > RAW_TEXT_LIMIT:
        dump = _dump_payload(message)
        logger.debug(f"{len(message)} chars > {RAW_TEXT_LIMIT} — file route ({dump.name})")
        fobj = await eng.upload_file_inpage(str(dump))
        files = [fobj]
        send_text = (message[:300]
                     + "\n\n[...full text attached as a document — read the file.]")

    if not chat_id:
        chat_id = await eng.create_chat(model_id)

    body = build_body(send_text, chat_id, parent_id, files=files, model=model_id,
                      thinking_mode=thinking_mode)
    url = f"{URL}?chat_id={chat_id}"
    logger.debug(f"in-page completions chat={chat_id[:8]}… model={model_id} chars={len(send_text)}")

    state: dict = {"chosen": None, "new_parent": parent_id, "saw_content": False}
    emitted = False
    buf = ""

    # Committed to the ghost transport — chat is created and the in-page fetch
    # is about to open. Emitted before any content so the status card shows the
    # real path even if the stream later stalls and falls back to curl.
    yield {"type": "status", "message": "qwen_transport_ghost"}

    async for sev in eng.stream(url, body):
        t = sev.get("t")
        if t == "reconnect":
            # Idle stall — a fresh connection is opening (not a fatal error yet).
            yield {"type": "status",
                   "message": f"retrying_stall_{sev.get('attempt', 2)}"}
        elif t == "chunk":
            data = sev.get("data", "")
            if "RGV587" in data:
                if emitted:
                    yield _ctx.error("ghost: captcha punish (RGV587)")
                    return
                raise GhostError("captcha punish (RGV587) — WAF flagged the payload")
            buf += data
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if not line.startswith("data: "):
                    if line:
                        try:
                            err = json.loads(line)
                            if err.get("success") is False or "error" in err:
                                raise GhostError(f"stream error event: {line[:200]}")
                        except json.JSONDecodeError:
                            pass
                    continue
                try:
                    d = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                events = _parse_sse(d, state)
                if events:
                    if not emitted:
                        emitted = True
                        yield {"type": "meta", "chat_id": chat_id, "parent_id": parent_id}
                        yield {"type": "status", "message": "calling_upstream"}
                        yield {"type": "request_sent"}
                    for ev in events:
                        yield ev
        elif t == "error":
            if emitted:
                yield _ctx.error(f"ghost: {sev.get('err')}")
                return
            raise GhostError(f"in-page fetch error: {sev.get('err')}")
        # t == "status" / "done": stream-level bookkeeping, nothing to emit

    if not emitted:
        raise GhostError("empty stream (silent reject / tarpit)")
    yield {"type": "done", "chat_id": chat_id, "parent_id": state["new_parent"]}
