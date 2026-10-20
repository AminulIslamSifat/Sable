"""Xiaomi MiMo Studio connector for Sable — browser-auth, streamed, multi-turn.

Rides the logged-in MiMo account via cookies pulled from Sable's shared
BrowserManager context. No API key, no bearer, no token provider — just three
cookies (xiaomichatbot_serviceToken, userId, xiaomichatbot_ph). The ph value is
also passed as the ?xiaomichatbot_ph= query param on every call.

Protocol (reverse-engineered, live capture 2026-09-22):
  POST {base}/bot/chat?xiaomichatbot_ph=<urlencoded ph>
  body: {msgId, conversationId, query, isEditedQuery,
         modelConfig: {enableThinking, webSearchStatus, model}, multiMedias}

Response = text/event-stream:
  event:dialogId  data:{"content":"13145251"}
  event:message   data:{"type":"text","content":"..."}

Model routing (base path depends on model):
  mimo-v2.6-pro                   -> /open-apis
  mimo-v2.6-flash                 -> /open-apis
  mimo-v2.6-pro-ultraspeed-studio -> /fastchat/open-apis

Upload (3 steps, no signing):
  1. POST {base}/resource/genUploadInfo {fileName, fileContentMd5}
  2. PUT  uploadUrl  (raw bytes)
  3. POST {base}/resource/parse?fileUrl=<urlencoded>  {}

Stop = client-side abort (SPA passes an AbortSignal; no stop endpoint).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from pathlib import Path
from typing import Any

from curl_cffi.requests import AsyncSession

logger = logging.getLogger("sable.mimo")

BASE_URL = "https://aistudio.xiaomimimo.com"

MODEL_ROUTES = {
    "mimo-v2.6-pro": "/open-apis",
    "mimo-v2.6-flash": "/open-apis",
    "mimo-v2.6-pro-ultraspeed-studio": "/fastchat/open-apis",
}

# Chat endpoint differs per model — the base path alone is not enough.
# Verified against a live browser capture (2026-10-20): the UltraSpeed model is
# served by the /fastchat backend's `bot/chat` route, NOT `react/chat`. The old
# hardcoded `/react/chat` 404'd for every /fastchat model, which is why
# UltraSpeed never worked while pro/flash did.
CHAT_ROUTES = {
    "mimo-v2.6-pro": "/react/chat",
    "mimo-v2.6-flash": "/react/chat",
    "mimo-v2.6-pro-ultraspeed-studio": "/bot/chat",
}
DEFAULT_MODEL = "mimo-v2.6-pro"

_ALIASES = {
    "pro": "mimo-v2.6-pro",
    "flash": "mimo-v2.6-flash",
    "ultra": "mimo-v2.6-pro-ultraspeed-studio",
    "ultraspeed": "mimo-v2.6-pro-ultraspeed-studio",
}


def _route_for(model: str | None) -> tuple[str, str]:
    m = (model or DEFAULT_MODEL).strip()
    m = _ALIASES.get(m.lower(), m)
    if m not in MODEL_ROUTES:
        m = DEFAULT_MODEL
    return MODEL_ROUTES[m], m


def _chat_path_for(model_id: str) -> str:
    """Chat endpoint for a resolved model id (see CHAT_ROUTES)."""
    return CHAT_ROUTES.get(model_id, "/react/chat")


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
        _instruction_cache = build_instructions(
            project_id=project_id, provider="mimo", layout_mode=layout_mode
        )
    return _instruction_cache or ""


class MiMoClient:
    """Browser-auth MiMo Studio client. Cookies come from an injected provider."""

    def __init__(self) -> None:
        from connectors.common.error_ctx import ErrorContext
        self.ctx = ErrorContext("mimo")
        self._cookie_provider: Callable[[], Awaitable[dict[str, str]]] | None = None
        self._http: AsyncSession | None = None
        self._jar: dict[str, str] | None = None
        self._jar_ts: float = 0.0
        self._active_resp = None
        self._chains: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._stream_lock = asyncio.Lock()

    # ---- wiring ----

    def set_cookie_provider(self, fn: Callable[[], Awaitable[dict[str, str]]]) -> None:
        self._cookie_provider = fn

    @property
    def is_available(self) -> bool:
        return self._cookie_provider is not None

    # ---- cookies ----

    async def _cookies(self, force: bool = False) -> dict[str, str]:
        if self._jar is not None and not force and (time.time() - self._jar_ts) < 300:
            return self._jar
        if self._cookie_provider is None:
            raise RuntimeError("MiMo cookie provider not set")
        jar = await self._cookie_provider()
        if not jar:
            raise RuntimeError("MiMo cookie provider returned no cookies")
        self._jar = jar
        self._jar_ts = time.time()
        return jar

    @staticmethod
    def _ph_from_jar(jar: dict[str, str]) -> str:
        return (jar.get("xiaomichatbot_ph", "") or "").strip('"')

    # ---- http ----

    async def _http_session(self) -> AsyncSession:
        if self._http is None:
            self._http = AsyncSession(impersonate="chrome", timeout=300)
        return self._http

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "x-timeZone": "Asia/Dhaka",
            "Origin": BASE_URL,
            "Referer": BASE_URL + "/",
        }

    async def _api_post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        jar = await self._cookies()
        ph = self._ph_from_jar(jar)
        # path may already carry a query string (e.g. parse?fileUrl=...) —
        # use & in that case, otherwise the ph lands inside the prior value.
        sep = "&" if "?" in path else "?"
        url = f"{BASE_URL}{path}{sep}xiaomichatbot_ph={ph}"
        http = await self._http_session()
        resp = await http.post(url, headers=self._headers(), json=body, cookies=jar)
        if resp.status_code != 200:
            raise RuntimeError(f"MiMo {path} HTTP {resp.status_code}: {resp.text[:200]}")
        out = json.loads(resp.text)
        if out.get("code") not in (0, None):
            raise RuntimeError(f"MiMo {path} code={out.get('code')} msg={out.get('msg')}")
        return out

    # ---- upload ----
    async def validate_auth(self) -> str:
        """Probe MiMo auth with a lightweight conversation/save call.

        This is the exact endpoint that 401s when the service token is dead,
        so it is a faithful liveness check. Returns the authenticated userId
        on success; raises RuntimeError with a human-readable reason on failure.
        """
        jar = await self._cookies(force=True)
        tok = (jar.get("xiaomichatbot_serviceToken") or "").strip('"')
        uid = (jar.get("userId") or "").strip('"')
        ph = self._ph_from_jar(jar)
        if not tok:
            raise RuntimeError("no xiaomichatbot_serviceToken cookie")
        if not uid:
            raise RuntimeError("no userId cookie")
        if not ph:
            raise RuntimeError("no xiaomichatbot_ph cookie")
        # Replacement chars mean cookie_reader could not DECRYPT the value and
        # shipped ciphertext. That is not an expired session — re-logging in
        # cannot fix it — so say so instead of sending the user round in
        # circles (the old message claimed "expired" for exactly this case).
        bad = sum(v.count("\ufffd") for v in (tok, uid, ph))
        if bad:
            raise RuntimeError(
                "MiMo cookies are undecryptable (%d replacement chars) — the "
                "profile uses an encryption scheme this reader cannot open. "
                "Re-login will NOT help; fix engine/cookie_reader.py or point "
                "the mimo provider at a profile with readable cookies." % bad
            )
        http = await self._http_session()
        url = f"{BASE_URL}/fastchat/open-apis/chat/conversation/save?xiaomichatbot_ph={ph}"
        body = {"conversationId": uuid.uuid4().hex, "title": "auth probe", "type": "chat"}
        resp = await http.post(url, headers=self._headers(), json=body, cookies=jar)
        if resp.status_code == 401:
            raise RuntimeError(
                "MiMo rejected the session token (401). The cookie is present "
                "but expired — log in to aistudio.xiaomimimo.com again in this "
                "account's browser profile."
            )
        if resp.status_code != 200:
            raise RuntimeError(f"auth probe HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            out = json.loads(resp.text)
        except Exception:
            out = {}
        code = out.get("code")
        if code == 401:
            raise RuntimeError(
                "MiMo rejected the session token (code=401). Log in again to "
                "aistudio.xiaomimimo.com in this account's browser profile."
            )
        if code not in (0, None):
            raise RuntimeError(f"auth probe code={code} msg={out.get('msg')}")
        return uid



    async def upload_file(self, path: str | Path) -> dict[str, Any]:
        """Upload a file and return the object for `multiMedias`."""
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"MiMo upload: no such file: {p}")
        data = p.read_bytes()
        md5 = hashlib.md5(data).hexdigest()
        mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        media_type = "image" if mime.startswith("image/") else "file"

        info = await self._api_post(
            "/open-apis/resource/genUploadInfo",
            {"fileName": p.name, "fileContentMd5": md5},
        )
        d = info.get("data") or {}
        upload_url = d.get("uploadUrl")
        resource_url = d.get("resourceUrl")
        if not upload_url or not resource_url:
            raise RuntimeError(f"MiMo genUploadInfo returned no url: {info}")

        http = await self._http_session()
        # FDS validates content-md5 against the signature — MUST send it.
        # Live capture showed the browser sends `content-md5: <hex md5>` and
        # `content-type: application/octet-stream` on the PUT.
        put = await http.put(
            upload_url,
            headers={"Content-Type": "application/octet-stream", "content-md5": md5},
            data=data,
        )
        if put.status_code not in (200, 201, 204):
            raise RuntimeError(f"MiMo upload PUT HTTP {put.status_code}: {put.text[:200]}")

        import urllib.parse as up
        parse = await self._api_post(
            "/open-apis/resource/parse?fileUrl=" + up.quote(resource_url, safe=""),
            {},
        )
        pd = parse.get("data") or {}

        return {
            "mediaType": media_type,
            "fileUrl": resource_url,
            "name": p.name,
            "size": len(data),
            "status": "completed",
            "objectName": d.get("resourceId", ""),
            "url": pd.get("id", ""),
            "tokenUsage": pd.get("tokenUsage", 0),
            "mime": mime,
        }

    # ---- stop ----

    async def cancel(self) -> bool:
        """Abort the in-flight bot/chat stream (the SPA's stop button)."""
        resp = getattr(self, "_active_resp", None)
        if resp is None:
            return False
        try:
            await resp.aclose()
            return True
        except Exception as exc:
            logger.debug("MiMo cancel failed: %s", exc)
            return False

    # ---- conversation ----

    async def _create_conversation(self, base: str, title: str = "New conversation") -> str:
        conv_id = uuid.uuid4().hex
        await self._api_post(
            f"{base}/chat/conversation/save",
            {"conversationId": conv_id, "title": title, "type": "chat"},
        )
        return conv_id

    async def _gen_title(self, base: str, conv_id: str, content: str) -> None:
        try:
            await self._api_post(
                f"{base}/chat/conversation/genTitle",
                {"conversationId": conv_id, "content": content},
            )
        except Exception as exc:
            logger.debug("MiMo genTitle skipped: %s", exc)

    # ---- SSE ----

    @staticmethod
    def _parse_sse_event(raw: str) -> tuple[str | None, str | None]:
        event = None
        data = None
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
        return event, data

    # ---- core stream ----

    async def _stream(
        self, message: str, *, chat_id: str | None, model: str,
        thinking: bool, web_search: bool,
        inject_instructions: bool, project_id: str | None, layout_mode: str | None,
        files: list[dict[str, Any]] | None = None,
        system_instruction: str | None = None,
        db_history: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        async with self._stream_lock:
            text = message
            chain = self._chains.get(chat_id) if chat_id else None
            if not chain:
                if inject_instructions:
                    # Caller-supplied system_instruction (study mode / agent
                    # sub-chats) WINS over the built default — same precedence
                    # as Kimi and DeepSeek. Without this, study and subagent
                    # chats silently fell back to the full chat persona.
                    instr = system_instruction or _load_instructions(project_id, layout_mode)
                    if instr:
                        text = instr + "\n\n" + text
                # Cross-provider switch: MiMo has no wire-level history field,
                # so seed prior turns into the query text on a fresh chain —
                # otherwise the model starts from scratch mid-conversation.
                if db_history:
                    from connectors.common.history_budget import trim_history_messages
                    _seeded = trim_history_messages([
                        {"role": m["role"], "content": m["content"]}
                        for m in db_history
                        if m.get("role") in ("user", "assistant") and m.get("content")
                    ])
                    if _seeded:
                        _hist_block = "\n".join(
                            f"[{m['role']}]: {m['content']}" for m in _seeded
                        )
                        text = f"[CONVERSATION HISTORY]\n{_hist_block}\n\n[USER MESSAGE]\n{text}"

            base, model_id = _route_for(model)
            try:
                jar = await self._cookies()
            except Exception as exc:
                yield self.ctx.error(f"auth failed: {exc}")
                return
            ph = self._ph_from_jar(jar)

            try:
                conv_id = chain.get("conv") if chain else None
                if not conv_id:
                    conv_id = await self._create_conversation(base)
            except Exception as exc:
                yield self.ctx.error(f"conversation setup failed: {exc}")
                return

            medias = []
            for f in (files or []):
                medias.append({k: v for k, v in f.items() if not k.startswith("_")})

            body = {
                "msgId": uuid.uuid4().hex,
                "conversationId": conv_id,
                "query": text,
                "isEditedQuery": False,
                "modelConfig": {
                    "enableThinking": bool(thinking),
                    "webSearchStatus": "enabled" if web_search else "disabled",
                    "model": model_id,
                },
                "multiMedias": medias,
            }

            url = f"{BASE_URL}{base}{_chat_path_for(model_id)}?xiaomichatbot_ph={ph}"
            headers = self._headers()
            headers["Accept"] = "text/event-stream"

            answer: list[str] = []
            thinking_buf: list[str] = []
            dialog_id: str | None = chain.get("dialog") if chain else None

            try:
                http = await self._http_session()
                resp = await http.post(url, headers=headers, json=body, cookies=jar, stream=True)
            except Exception as exc:
                yield self.ctx.error(f"request failed: {exc}")
                return

            self._active_resp = resp
            if resp.status_code != 200:
                self._active_resp = None
                yield self.ctx.error(f"HTTP {resp.status_code}: {resp.text[:200]}", status_code=resp.status_code)
                return

            buf = ""
            in_think = False
            # MiMo re-sends an OVERLAPPING TAIL of the answer rather than a pure
            # delta (e.g. content "INSTR_Q" then "STR_Q"). Appending blindly
            # duplicates the tail, so merge on the longest suffix/prefix overlap.
            _acc_content = ""
            from engine.think_parser import ThinkTagSplitter
            _think_split = ThinkTagSplitter()
            try:
                async for chunk in resp.aiter_content():
                    if not chunk:
                        continue
                    buf += chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else chunk
                    while "\n\n" in buf:
                        block, buf = buf.split("\n\n", 1)
                        event, data = self._parse_sse_event(block)
                        if event == "dialogId" and data:
                            try:
                                dialog_id = json.loads(data).get("content") or dialog_id
                            except Exception:
                                pass
                            continue
                        if event == "finish":
                            # {"content":"[DONE]"} — end of stream marker.
                            continue
                        if event != "message" or not data:
                            continue
                        try:
                            msg = json.loads(data)
                        except Exception:
                            continue
                        # Current protocol (/react/chat) tags each frame with
                        # `messageType` and puts chain-of-thought in
                        # `reasoningContent` (the old /bot/chat used
                        # `type: "text"`, which no longer exists — matching on
                        # it silently dropped every frame).
                        content = msg.get("content", "") or ""
                        reasoning = msg.get("reasoningContent", "") or ""
                        if reasoning:
                            thinking_buf.append(reasoning)
                            yield {"type": "thinking", "text": reasoning}
                        if not content:
                            continue
                        # strip stray NUL markers MiMo sends as separators
                        content = content.replace("\x00", "")
                        if not content:
                            continue
                        # Collapse the overlap: if this frame re-sends text we
                        # already have, only emit the genuinely new tail.
                        if _acc_content:
                            if _acc_content.endswith(content):
                                continue  # fully redundant resend
                            for _n in range(min(len(_acc_content), len(content)), 0, -1):
                                if _acc_content.endswith(content[:_n]):
                                    content = content[_n:]
                                    break
                            if not content:
                                continue
                        _acc_content += content
                        # Route through the shared splitter so <think> blocks
                        # (even split across chunks) become thinking events.
                        for _kind, _seg in _think_split.feed(content):
                            if _kind == "thinking":
                                thinking_buf.append(_seg)
                                yield {"type": "thinking", "text": _seg}
                            else:
                                answer.append(_seg)
                                yield {"type": "answer", "text": _seg}
            except Exception as exc:
                # A cancel() closes the response handle mid-iteration — that's
                # expected, not an error. Any content we already streamed stands.
                self._active_resp = None
                if not answer and not thinking_buf:
                    yield self.ctx.error(f"stream read failed: {exc}")
                    return
                logger.debug("MiMo stream interrupted (likely cancel): %s", exc)

            self._active_resp = None
            full = "".join(answer)
            if chat_id:
                self._chains[chat_id] = {"conv": conv_id, "dialog": dialog_id}
            if not chain:
                asyncio.create_task(self._gen_title(base, conv_id, (text[:200] + " " + full[:200])))

            yield {"type": "done", "parent_id": dialog_id or conv_id}

    async def stream_chat(
        self, message: str, *, model: str | None = None,
        thinking_mode: str | None = None, chat_id: str | None = None,
        ref_file_ids: list[str] | None = None,
        ref_files: list[dict[str, Any]] | None = None,
        inject_instructions: bool = True,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        m = (model or DEFAULT_MODEL).lower()
        if thinking_mode is None:
            thinking = "flash" not in m
        else:
            thinking = thinking_mode.lower() not in ("fast", "off", "", "disabled")
        web_search = bool(kwargs.get("web_search", False))
        # Same contract as Kimi/DeepSeek: caller-supplied system prompt wins on a
        # fresh chain, and DB history seeds the conversation after a provider switch.
        system_instruction = kwargs.pop("system_instruction", None)
        db_history = kwargs.pop("db_history", None)

        files_payload: list[dict[str, Any]] | None = ref_files or None
        if files_payload is None and ref_file_ids:
            files_payload = [{"mediaType": "file", "fileUrl": fid, "name": "file"}
                             for fid in ref_file_ids if fid] or None

        try:
            from connectors.common.jitter import jitter_for as _jitter_for, sleep_jitter as _sleep_jitter
            _jd = _jitter_for("mimo")
            if _jd:
                yield {"type": "status", "message": f"mimo_delay_{_jd:.1f}s"}
                await _sleep_jitter(_jd)
        except Exception:
            pass

        async for ev in self._stream(
            message, chat_id=chat_id, model=model or DEFAULT_MODEL,
            thinking=thinking, web_search=web_search,
            inject_instructions=inject_instructions,
            project_id=kwargs.get("project_id"), layout_mode=kwargs.get("layout_mode"),
            files=files_payload,
            system_instruction=system_instruction,
            db_history=db_history,
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

    @property
    def is_available(self) -> bool:
        return self._cookie_provider is not None

    def invalidate_jar(self) -> None:
        """Drop the cached cookie jar so the next request re-reads the live
        browser profile (used by the Refresh Token action)."""
        self._jar = None
        self._jar_ts = 0.0

    def reset_chain(self, chat_id: str) -> None:
        self._chains.pop(chat_id, None)

    async def close(self) -> None:
        if self._http is not None:
            try:
                await self._http.close()
            except Exception:
                pass
            self._http = None


_client: MiMoClient | None = None


def get_client() -> MiMoClient:
    global _client
    if _client is None:
        _client = MiMoClient()
    return _client
