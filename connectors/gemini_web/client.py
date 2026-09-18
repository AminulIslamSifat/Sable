"""Gemini Web connector for Sable — browser-auth, streamed, multi-turn.

Rides the logged-in Google account via cookies pulled from Sable's shared
BrowserManager context. No API key. Pure HTTP send via curl_cffi; the browser
is only asked for cookies.

Protocol (reverse-engineered, build boq_assistant-bard-web-server_20260914.08_p0):
  POST /_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate
    ?bl=<build>&f.sid=<session>&hl=en-US&_reqid=<n>&rt=c
  body: f.req=<urlencoded [null, "<inner-json>"]>&

  inner[0][0]  = user message
  inner[2]     = [c_id, r_id, rc_id, ..., continuation_token]  (empty for fresh)
  inner[17]    = [[turn_counter]]

Response = length-prefixed frames:
  )]}'
  <len>\n<json>   (repeated)
  Each <json> = [["wrb.fr", null, "<escaped-inner-json>"]]
  Text frames carry cumulative full text; we emit deltas.

Yields Sable events:
  {"type": "answer",   "text": "..."}
  {"type": "thinking", "text": "..."}
  {"type": "done",     "parent_id": "..."}
  {"type": "error",    "message": "..."}
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from pathlib import Path
from typing import Any

from curl_cffi.requests import AsyncSession

logger = logging.getLogger("sable.gemini_web")

_SYSTEM_DIR = Path(__file__).resolve().parent.parent.parent / "system"
TEMPLATE_PATH = _SYSTEM_DIR / ".gemini_web_template.json"
COOKIE_URL = "https://gemini.google.com"
REQID_STEP = 400000

# Instruction injection (mirrors deepseek/zai connectors)
_instruction_cache: str | None = None
_cached_key: tuple = ("__none__", "__none__", -1)


def _load_instructions(project_id: str | None, layout_mode: str | None) -> str:
    global _instruction_cache, _cached_key
    try:
        from connectors.common.instruction_builder import build_instructions, get_instruction_version
    except Exception:
        return ""
    ver = get_instruction_version()
    key = (project_id or "__none__", layout_mode or "__none__", ver)
    if key != _cached_key:
        _instruction_cache = None
        _cached_key = key
    if _instruction_cache is None:
        _instruction_cache = build_instructions(project_id=project_id, provider="gemini_web", layout_mode=layout_mode)
    return _instruction_cache or ""


# ── frame parsing ──────────────────────────────────────────────────────────

def _parse_frames(text: str) -> list[Any]:
    """Decode length-prefixed StreamGenerate frames into inner JSON objects."""
    out: list[Any] = []
    lines = text.split("\n")
    i = 0
    # skip anti-hijack preamble
    while i < len(lines) and not lines[i].strip().isdigit():
        i += 1
    while i < len(lines):
        head = lines[i].strip()
        if not head.isdigit():
            i += 1
            continue
        i += 1
        if i >= len(lines):
            break
        payload = lines[i]
        i += 1
        try:
            arr = json.loads(payload)
        except Exception:
            continue
        for entry in arr if isinstance(arr, list) else []:
            if not (isinstance(entry, list) and len(entry) >= 3):
                continue
            if entry[0] != "wrb.fr" or not isinstance(entry[2], str):
                continue
            try:
                out.append(json.loads(entry[2]))
            except Exception:
                continue
    return out


def _extract_text(inner: Any) -> str:
    """Pull cumulative answer text out of a parsed inner frame."""
    return _extract_candidate(inner)[1]


def _extract_candidate(inner: Any) -> tuple[str | None, str]:
    """Return (rc_id, cumulative_text) from a text frame.

    Text frames look like: [null, [...], null, null, [["rc_xxx", ["full text"], ...]]]
    The rc_ id is the candidate id the NEXT turn must echo back in inner[2][2].
    """
    try:
        cand = inner[4]
        if isinstance(cand, list) and cand:
            first = cand[0]
            if isinstance(first, list) and len(first) >= 2:
                rc = first[0] if isinstance(first[0], str) else None
                txt = "".join(str(x) for x in first[1]) if isinstance(first[1], list) else ""
                return rc, txt
    except Exception:
        pass
    return None, ""


# ── client ─────────────────────────────────────────────────────────────────

class GeminiWebClient:
    """Browser-auth Gemini client. Cookies come from an injected provider."""

    def __init__(self) -> None:
        self._cookie_provider: Callable[[], Awaitable[dict[str, str]]] | None = None
        self._http: AsyncSession | None = None
        self._jar: dict[str, str] | None = None
        self._jar_ts: float = 0.0
        self._at: str | None = None
        self._template: dict[str, Any] | None = None
        self._reqid: int = 0
        # chat_id -> {c, r, rc, token, turn}
        self._chains: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._stream_lock = asyncio.Lock()

    # ---- wiring ----

    def set_cookie_provider(self, fn: Callable[[], Awaitable[dict[str, str]]]) -> None:
        self._cookie_provider = fn

    @property
    def is_available(self) -> bool:
        return TEMPLATE_PATH.is_file()

    # ---- template ----

    def _load_template(self) -> dict[str, Any]:
        if self._template is None:
            self._template = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
        return self._template

    # ---- cookies ----

    def invalidate_jar(self) -> None:
        """Drop the cached cookie jar so the next call re-reads from browser."""
        self._jar = None
        self._jar_ts = 0.0
        self._at = None

    async def _cookies(self, force: bool = False) -> dict[str, str]:
        if not force and self._jar and (time.time() - self._jar_ts) < 1800:
            return self._jar
        if self._cookie_provider is None:
            raise RuntimeError("No cookie provider wired — cannot auth")
        jar = await self._cookie_provider()
        if not jar or "SAPISID" not in jar and "__Secure-1PSID" not in jar:
            raise RuntimeError("Cookie provider returned no Google auth cookies")
        self._jar = jar
        self._jar_ts = time.time()
        return jar

    async def _xsrf(self, jar: dict[str, str], force: bool = False) -> str:
        """Fetch the SNlM0e XSRF token StreamGenerate needs as `at=`.

        It is embedded in the gemini.google.com page HTML and rotates per
        session, so it is cached next to the jar and re-read whenever the jar
        refreshes or a send comes back 400.
        """
        if not force and self._at:
            return self._at
        http = await self._http_session()
        resp = await http.get(COOKIE_URL + "/app", cookies=jar)
        m = re.search(r'SNlM0e"?\s*:\s*"([^"]+)"', resp.text)
        if not m:
            raise RuntimeError("could not extract SNlM0e XSRF token from Gemini page")
        self._at = m.group(1)
        return self._at

    def _next_reqid(self) -> int:
        tpl = self._load_template()
        if not self._reqid:
            self._reqid = int(tpl.get("query", {}).get("_reqid", 2000000))
        self._reqid += REQID_STEP
        return self._reqid

    # ---- http ----

    async def _http_session(self) -> AsyncSession:
        if self._http is None:
            self._http = AsyncSession(impersonate="chrome", timeout=300)
        return self._http

    def _build_url(self) -> str:
        tpl = self._load_template()
        q = dict(tpl.get("query", {}))
        q["_reqid"] = str(self._next_reqid())
        qs = "&".join(f"{k}={v}" for k, v in q.items())
        return f"{tpl['base_url']}?{qs}"

    def _build_body(self, inner: list[Any], at: str | None = None) -> str:
        import urllib.parse as up
        outer = [None, json.dumps(inner, separators=(",", ":"))]
        body = "f.req=" + up.quote(json.dumps(outer, separators=(",", ":"))) + "&"
        if at:
            body += "at=" + up.quote(at)
        return body

    # ---- chain state ----

    def _fresh_inner(self, message: str) -> list[Any]:
        tpl = self._load_template()
        inner = json.loads(json.dumps(tpl["inner"]))  # deep copy
        inner[0][0] = message
        inner[2] = ["", "", "", None, None, None, None, None, None, ""]
        inner[17] = [[0]]
        return inner

    def _resume_inner(self, message: str, chain: dict[str, Any]) -> list[Any]:
        tpl = self._load_template()
        inner = json.loads(json.dumps(tpl["inner"]))
        inner[0][0] = message
        inner[2] = [
            chain.get("c", ""), chain.get("r", ""), chain.get("rc", ""),
            None, None, None, None, None, None, chain.get("token", ""),
        ]
        inner[17] = [[int(chain.get("turn", 0))]]
        return inner

    # ---- core stream ----

    async def _stream(
        self, message: str, *, chat_id: str | None,
        inject_instructions: bool, project_id: str | None, layout_mode: str | None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        async with self._stream_lock:
            text = message
            chain = self._chains.get(chat_id) if chat_id else None
            if inject_instructions and not chain:
                instr = _load_instructions(project_id, layout_mode)
                if instr:
                    text = instr + "\n\n" + text

            inner = self._resume_inner(text, chain) if chain else self._fresh_inner(text)
            url = self._build_url()
            tpl = self._load_template()
            headers = {k: v for k, v in tpl.get("headers", {}).items() if k.lower() != "content-length"}
            headers["content-type"] = "application/x-www-form-urlencoded;charset=UTF-8"

            new_chain: dict[str, Any] = dict(chain or {})
            emitted = 0
            got_frame = False

            try:
                jar = await self._cookies()
                at = await self._xsrf(jar)
                http = await self._http_session()
                body = self._build_body(inner, at)
                resp = await http.post(url, headers=headers, data=body,
                                       cookies=jar, stream=True)
                # XSRF token rotates mid-session — re-read once on a 400.
                if resp.status_code == 400:
                    at = await self._xsrf(jar, force=True)
                    body = self._build_body(inner, at)
                    resp = await http.post(url, headers=headers, data=body,
                                           cookies=jar, stream=True)
            except Exception as exc:
                yield {"type": "error", "message": f"Gemini web request failed: {exc}"}
                return

            if resp.status_code != 200:
                yield {"type": "error", "message": f"HTTP {resp.status_code}: {resp.text[:200]}"}
                return

            # Stream raw bytes and slice frames ourselves. curl_cffi's
            # aiter_lines() buffers the whole response, which kills streaming;
            # aiter_content yields real network chunks. Frame wire format:
            #   )]}'\n<len>\n<json>\n<len>\n<json>\n...
            # so we keep a buffer, pull complete <len>\n<json> pairs out of it.
            # Accumulate bytes and re-run the PROVEN frame parser over the whole
            # buffer each tick, emitting only newly-complete frames. Parsing the
            # full buffer each time is cheap (frames are small) and sidesteps all
            # length/encoding slicing bugs.
            raw_buf = ""
            seen = 0
            try:
                async for chunk in resp.aiter_content():
                    if not chunk:
                        continue
                    raw_buf += chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else chunk
                    frames = _parse_frames(raw_buf)
                    if len(frames) <= seen:
                        continue
                    for inner_frame in frames[seen:]:
                        got_frame = True
                        # chain ids: [null, [c_id, r_id], ...]
                        try:
                            ids = inner_frame[1]
                            if isinstance(ids, list) and len(ids) >= 2:
                                if ids[0]:
                                    new_chain["c"] = ids[0]
                                if ids[1]:
                                    new_chain["r"] = ids[1]
                        except Exception:
                            pass
                        # metadata dict: continuation token ("26") + rc pool ("21")
                        try:
                            meta = inner_frame[2]
                            if isinstance(meta, dict):
                                if meta.get("26"):
                                    new_chain["token"] = meta["26"]
                                pool = meta.get("21")
                                if isinstance(pool, list) and pool and isinstance(pool[0], str):
                                    if pool[0].startswith("rc_"):
                                        new_chain["rc"] = pool[0]
                        except Exception:
                            pass
                        # text frame -> rc id + cumulative text
                        rc, full = _extract_candidate(inner_frame)
                        if rc:
                            new_chain["rc"] = rc
                        if full and len(full) > emitted:
                            delta = full[emitted:]
                            emitted = len(full)
                            yield {"type": "answer", "text": delta}
                    seen = len(frames)
            except Exception as exc:
                if not got_frame:
                    yield {"type": "error", "message": f"Stream read failed: {exc}"}
                    return

            if not got_frame:
                yield {"type": "error", "message": "No parseable frames in response"}
                return

            new_chain["turn"] = int(new_chain.get("turn", 0)) + 1
            if chat_id:
                self._chains[chat_id] = new_chain
            pid = new_chain.get("r") or uuid.uuid4().hex
            yield {"type": "done", "parent_id": pid}

    async def stream_chat(
        self, message: str, *, model: str | None = None,
        thinking_mode: str | None = None, chat_id: str | None = None,
        ref_file_ids: list[str] | None = None, inject_instructions: bool = True,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        # Anti-burst jitter: one gap per send (each tool-call round / user turn).
        try:
            from connectors.common.jitter import jitter_for as _jitter_for, sleep_jitter as _sleep_jitter
            _jdelay = _jitter_for("gemini")
            if _jdelay:
                # Emit status BEFORE sleeping so the UI card updates first
                yield {"type": "status", "message": f"gemini_delay_{_jdelay:.1f}s"}
                await _sleep_jitter(_jdelay)
        except Exception as _jexc:
            logger.debug("Gemini jitter skipped: %s", _jexc)

        project_id = kwargs.get("project_id")
        layout_mode = kwargs.get("layout_mode")
        async for ev in self._stream(
            message, chat_id=chat_id, inject_instructions=inject_instructions,
            project_id=project_id, layout_mode=layout_mode,
        ):
            yield ev

    async def chat(self, message: str, **kwargs: Any) -> dict[str, Any]:
        answer, thinking, parent_id, error = [], [], None, None
        async for ev in self.stream_chat(message, **kwargs):
            t = ev.get("type")
            if t == "answer":
                answer.append(ev.get("text", ""))
            elif t == "thinking":
                thinking.append(ev.get("text", ""))
            elif t == "done":
                parent_id = ev.get("parent_id")
            elif t == "error":
                error = ev.get("message")
        return {"answer": "".join(answer), "thinking": "".join(thinking),
                "parent_id": parent_id, "error": error}

    def reset_chain(self, chat_id: str) -> None:
        self._chains.pop(chat_id, None)

    async def close(self) -> None:
        if self._http is not None:
            try:
                await self._http.close()
            except Exception:
                pass
            self._http = None


_client: GeminiWebClient | None = None


def get_client() -> GeminiWebClient:
    global _client
    if _client is None:
        _client = GeminiWebClient()
    return _client
