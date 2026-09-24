"""Mistral (Web) connector for Sable - chat.mistral.ai (Vibe), browser-auth, SSE.

Auth is cookie-only (Ory Kratos session): ory_session_*, ory_kratos_continuity,
csrf_token_*. No bearer, no API key. Cookies are read lock-free from the
pinned/active browser profile by ChatService.get_mistral_web_cookies.

Protocol (reverse-engineered, live capture 2026-09-24):
  POST https://chat.mistral.ai/api/chat  ->  text/event-stream (tRPC frames)
  first turn : {content:[{type:text,text}], mode:create, files:[], ...}
  follow-up  : {chatId, mode:append, messageInput:[...], messageId, messageFiles:[...]}

Response frames are tRPC JSON lines, each '<tag>:<json>':
  16:{...disclaimers...}                       bootstrap
  15:{json:{type:message,...,patches:[...]}}   JSON-patch deltas
  8:null                                        terminator

Patch ops (JSON-Patch):
  replace /                        seed assistant message (id, turn, chatId)
  replace /contentChunks           first chunk (reasoning if _context.type==reasoning)
  add     /contentChunks/N         new chunk (answer chunks carry _context:null)
  append  /contentChunks/N/text    token delta for chunk N
  replace /contentChunks/N/_context  context tag update

Thinking is enabled by adding the beta-reasoning feature flag; reasoning deltas
arrive in their own chunk tagged _context.type == 'reasoning'.

Upload (3 steps, Azure Blob):
  1. POST /api/trpc/file.uploadFile  {type,count,includeReadUrl} -> presigned url
  2. PUT  <presigned url>            raw bytes
  3. POST /api/trpc/file.confirmUpload {uploadUrl,type}
  then reference in /api/chat as messageFiles:[{type,url,name}]

Stop:
  POST /api/trpc/chat.cancelGeneration,message.maybeShowSatisfactionPulse?batch=1
  body {0:{chatId}, 1:{chatId,messageId,messageVersion}}
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import mimetypes
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from pathlib import Path
from typing import Any

from curl_cffi.requests import AsyncSession

logger = logging.getLogger('sable.mistral_web')

BASE_URL = 'https://chat.mistral.ai'
CHAT_URL = BASE_URL + '/api/chat'
DEFAULT_MODEL = 'mistral-web'

_BASE_FEATURES = ['beta-code-interpreter', 'beta-imagegen', 'beta-trampoline', 'beta-websearch']
_REASONING_FEATURE = 'beta-reasoning'

_TASK_CALLBACKS = [
    'ask_user_question', 'ask_user_confirmation', 'ask_enable_skill',
    'enable_connector', 'ask_retry_or_continue_rate_limit',
    'collect_workflow_input', 'delegate_workflow_execution',
]

_instruction_cache: str | None = None
_cached_key: tuple = ('__none__', '__none__', -1)


def _load_instructions(project_id: str | None, layout_mode: str | None) -> str:
    global _instruction_cache, _cached_key
    try:
        from connectors.common.instruction_builder import build_instructions, get_instruction_version
    except Exception:
        return ''
    ver = get_instruction_version()
    key = (project_id or '__none__', layout_mode or '__none__', ver)
    if key != _cached_key:
        _instruction_cache = None
        _cached_key = key
    if _instruction_cache is None:
        _instruction_cache = build_instructions(
            project_id=project_id, provider='mistral_web', layout_mode=layout_mode
        )
    return _instruction_cache or ''


def _mime_bucket(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or ''
    if mime.startswith('image/'):
        return 'image'
    if mime.startswith('text/'):
        return 'text'
    return 'document'


class MistralWebClient:
    """Browser-auth Mistral Vibe client. Cookies come from an injected provider."""

    def __init__(self) -> None:
        from connectors.common.error_ctx import ErrorContext
        self.ctx = ErrorContext("mistral_web")
        self._cookie_provider: Callable[[], Awaitable[dict[str, str]]] | None = None
        self._http: AsyncSession | None = None
        self._jar: dict[str, str] | None = None
        self._jar_ts: float = 0.0
        self._active_resp = None
        self._chains: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    # ---- wiring ----

    def set_cookie_provider(self, fn: Callable[[], Awaitable[dict[str, str]]]) -> None:
        self._cookie_provider = fn

    @property
    def is_available(self) -> bool:
        return self._cookie_provider is not None

    def invalidate_jar(self) -> None:
        self._jar = None

    # ---- cookies ----

    async def _cookies(self, force: bool = False) -> dict[str, str]:
        if self._jar is not None and not force and (time.time() - self._jar_ts) < 300:
            return self._jar
        if self._cookie_provider is None:
            raise RuntimeError('Mistral (Web) cookie provider not set')
        jar = await self._cookie_provider()
        if not jar:
            raise RuntimeError('Mistral (Web) cookie provider returned no cookies')
        self._jar = jar
        self._jar_ts = time.time()
        return jar

    # ---- http ----

    async def _http_session(self) -> AsyncSession:
        if self._http is None:
            self._http = AsyncSession(impersonate='chrome', timeout=300)
        return self._http

    def _headers(self) -> dict[str, str]:
        return {
            'Content-Type': 'application/json',
            'Accept': 'text/event-stream',
            'Origin': BASE_URL,
            'Referer': BASE_URL + '/chat',
        }

    async def _trpc_post(self, procedures: str, payload: dict[str, Any]) -> Any:
        jar = await self._cookies()
        http = await self._http_session()
        url = BASE_URL + '/api/trpc/' + procedures + '?batch=1'
        resp = await http.post(url, headers=self._headers(), json=payload, cookies=jar)
        if resp.status_code != 200:
            raise RuntimeError('Mistral tRPC ' + procedures + ' HTTP ' + str(resp.status_code) + ': ' + resp.text[:200])
        return _parse_trpc_body(resp.text)

    async def validate_auth(self) -> str:
        """Probe auth with a lightweight session call. Raises on failure."""
        jar = await self._cookies(force=True)
        http = await self._http_session()
        url = BASE_URL + '/api/trpc/user.session?batch=1&input=%7B%7D'
        resp = await http.get(url, headers={'Accept': 'application/json', 'Referer': BASE_URL + '/chat'}, cookies=jar)
        if resp.status_code != 200:
            raise RuntimeError('Mistral session HTTP ' + str(resp.status_code))
        low = resp.text.lower()
        if '"user":null' in low or 'unauthorized' in low:
            raise RuntimeError('Mistral session is not authenticated')
        return 'ok'

    # ---- upload ----

    async def upload_file(self, path: str | Path) -> dict[str, Any]:
        """Upload a file; returns the {type,url,name} object for messageFiles."""
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError('Mistral upload: no such file: ' + str(p))
        bucket = _mime_bucket(p)
        info = await self._trpc_post(
            'file.uploadFile',
            {'0': {'json': {'type': bucket, 'count': 1, 'includeReadUrl': True}}},
        )
        # Live capture (2026-09-24): the response is
        #   {uploadURLs:[...], readURLs:[...], uploadHeaders:{...}}
        # uploadURLs[0] is the Azure presigned PUT target; readURLs[0] is the
        # signed GET url the chat references in messageFiles[].url.
        upload_url = _first_url(info, 'uploadURLs', 'uploadUrls', 'uploadUrl', 'uploadURL')
        read_url = _first_url(info, 'readURLs', 'readUrls', 'readUrl', 'readURL') or upload_url
        if not upload_url:
            raise RuntimeError('Mistral uploadFile returned no url: ' + str(info)[:300])
        data = p.read_bytes()
        mime = mimetypes.guess_type(p.name)[0] or 'application/octet-stream'
        http = await self._http_session()
        put = await http.put(upload_url, headers={'Content-Type': mime, 'x-ms-blob-type': 'BlockBlob'}, data=data)
        if put.status_code not in (200, 201, 202, 204):
            raise RuntimeError('Mistral upload PUT HTTP ' + str(put.status_code) + ': ' + put.text[:200])
        await self._trpc_post(
            'file.confirmUpload',
            {'0': {'json': {'uploadUrl': upload_url, 'type': bucket}}},
        )
        return {'type': bucket, 'url': read_url, 'name': p.name}

    # ---- stop ----

    async def cancel(self, chat_id: str | None = None) -> bool:
        """Stop generation (SPA stop button) via the tRPC batch endpoint."""
        chain = self._chains.get(chat_id or '') or {}
        upstream = chain.get('chatId')
        msg_id = chain.get('messageId')
        version = chain.get('messageVersion', 0)
        resp = getattr(self, '_active_resp', None)
        if resp is not None:
            try:
                await resp.aclose()
            except Exception:
                pass
        if not upstream or not msg_id:
            return resp is not None
        try:
            jar = await self._cookies()
            http = await self._http_session()
            url = BASE_URL + '/api/trpc/chat.cancelGeneration,message.maybeShowSatisfactionPulse?batch=1'
            payload = {
                '0': {'json': {'chatId': upstream}},
                '1': {'json': {'chatId': upstream, 'messageId': msg_id, 'messageVersion': version}},
            }
            r = await http.post(url, headers=self._headers(), json=payload, cookies=jar)
            logger.info('Mistral cancel: chat=%s status=%s', chat_id, r.status_code)
            return r.status_code == 200
        except Exception as exc:
            logger.warning('Mistral cancel failed: %s', exc)
            return False

    # ---- streaming ----

    async def _stream(
        self,
        message: str,
        *,
        chat_id: str | None,
        thinking: bool,
        files: list[dict[str, Any]] | None,
        inject_instructions: bool,
        project_id: str | None,
        layout_mode: str | None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        jar = await self._cookies()
        http = await self._http_session()

        chain = self._chains.get(chat_id or '') if chat_id else None
        upstream_chat = chain.get('chatId') if chain else None
        is_fresh = upstream_chat is None

        features = list(_BASE_FEATURES)
        if thinking:
            features.append(_REASONING_FEATURE)

        msg_id = str(uuid.uuid4())
        if is_fresh:
            prompt = message
            if inject_instructions:
                instr = _load_instructions(project_id, layout_mode)
                if instr:
                    prompt = instr + '\n\n' + message
            body: dict[str, Any] = {
                'content': [{'type': 'text', 'text': prompt}],
                'transcriptionsMetadata': [],
                'incognito': False,
                'files': files or [],
                'features': features,
                'integrations': [],
                'libraries': [],
                'reviewComments': [],
                'mode': 'create',
                'productType': 'chat',
                'clientPromptData': _prompt_data(),
                'disabledFeatures': [],
                'supportedTaskCallbacks': _TASK_CALLBACKS,
                'messageId': msg_id,
            }
        else:
            body = {
                'chatId': upstream_chat,
                'mode': 'append',
                'messageInput': [{'type': 'text', 'text': message}],
                'messageFiles': files or [],
                'messageId': msg_id,
                'features': features,
                'libraries': [],
                'integrations': [],
                'disabledFeatures': [],
                'reviewComments': [],
                'preventFeaturesAndIntegrationsPersistence': False,
                'transcriptionsMetadata': [],
                'clientPromptData': _prompt_data(),
                'supportedTaskCallbacks': _TASK_CALLBACKS,
            }

        got_answer = False
        upstream_chat_id = upstream_chat
        live_msg_id: str | None = None
        live_msg_ver = 0
        chunk_kind: dict[int, str] = {}

        for attempt in range(2):
            state: dict[str, Any] = {'chat_id': upstream_chat, 'done': False, 'error': None, 'ctx': self.ctx}
            try:
                resp = await http.post(CHAT_URL, headers=self._headers(), json=body, cookies=jar, stream=True)
            except Exception as exc:
                yield self.ctx.error('request failed: ' + str(exc))
                return
            if resp.status_code != 200:
                # 429 / 401 / 403 are terminal — retrying immediately just deepens
                # the lockout. Surface and stop. Only genuine 5xx is worth one retry.
                if resp.status_code in (429, 401, 403):
                    yield self.ctx.error('HTTP ' + str(resp.status_code) + ': ' + resp.text[:200], status_code=resp.status_code)
                    return
                yield self.ctx.error('HTTP ' + str(resp.status_code), status_code=resp.status_code)
                return
            self._active_resp = resp
            buf = ''
            try:
                async for chunk in resp.aiter_content():
                    if not chunk:
                        continue
                    buf += chunk.decode('utf-8', 'replace') if isinstance(chunk, bytes) else chunk
                    while '\n' in buf:
                        line, buf = buf.split('\n', 1)
                        line = line.strip()
                        if not line:
                            continue
                        obj = _decode_frame(line)
                        if obj is None:
                            continue
                        for ev in _frame_to_events(obj, state, chunk_kind):
                            if ev['type'] == 'answer' and ev.get('text'):
                                got_answer = True
                            if state.get('chat_id'):
                                upstream_chat_id = state['chat_id']
                            if state.get('assistant_id'):
                                live_msg_id = state['assistant_id']
                            if state.get('assistant_version') is not None:
                                live_msg_ver = state['assistant_version']
                            if ev['type'] == 'done':
                                self._active_resp = None
                                yield ev
                                self._remember(chat_id, upstream_chat_id, live_msg_id or msg_id, live_msg_ver)
                                return
                            if ev['type'] == 'error':
                                self._active_resp = None
                                yield ev
                                return
                            yield ev
            except Exception as exc:
                self._active_resp = None
                if not got_answer:
                    yield self.ctx.error('stream failed: ' + str(exc))
                    return
                logger.debug('Mistral stream interrupted (likely cancel): %s', exc)
            self._active_resp = None
            if got_answer:
                break
            logger.warning('Mistral empty stream - retrying once')

        self._remember(chat_id, upstream_chat_id, live_msg_id or msg_id, live_msg_ver)
        yield {'type': 'done', 'chat_id': chat_id}

    def _remember(self, chat_id, upstream_chat_id, msg_id, version) -> None:
        if chat_id and upstream_chat_id:
            self._chains[chat_id] = {
                'chatId': upstream_chat_id,
                'messageId': msg_id,
                'messageVersion': version,
            }

    async def stream_chat(
        self, message: str, *, model: str | None = None,
        thinking_mode: str | None = None, chat_id: str | None = None,
        ref_file_ids: list[str] | None = None,
        ref_files: list[dict[str, Any]] | None = None,
        inject_instructions: bool = True,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        thinking = bool(thinking_mode) and thinking_mode.lower() not in ('fast', 'off', '', 'disabled', 'none')
        files_payload = ref_files or None
        async for ev in self._stream(
            message, chat_id=chat_id, thinking=thinking, files=files_payload,
            inject_instructions=inject_instructions,
            project_id=kwargs.get('project_id'), layout_mode=kwargs.get('layout_mode'),
        ):
            yield ev

    async def chat(self, message: str, **kwargs: Any) -> dict[str, Any]:
        answer: list[str] = []
        thinking_parts: list[str] = []
        error = None
        async for ev in self.stream_chat(message, **kwargs):
            t = ev.get('type')
            if t == 'answer':
                answer.append(ev.get('text', ''))
            elif t == 'thinking':
                thinking_parts.append(ev.get('text', ''))
            elif t == 'error':
                error = ev.get('message')
        return {'answer': ''.join(answer), 'thinking': ''.join(thinking_parts),
                'parent_id': None, 'error': error}


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _prompt_data() -> dict[str, str]:
    return {
        'currentDate': _dt.date.today().isoformat(),
        'userTimezone': 'T+06:00 (Asia/Dhaka)',
    }


def _decode_frame(line: str) -> dict[str, Any] | None:
    """Decode one tRPC line '<tag>:<json>' into its json object (or None)."""
    if ':' not in line:
        return None
    _tag, _sep, rest = line.partition(':')
    rest = rest.strip()
    if not rest or rest == 'null':
        return None
    try:
        obj = json.loads(rest)
    except Exception:
        return None
    if isinstance(obj, dict) and 'json' in obj:
        return obj['json']
    return obj if isinstance(obj, dict) else None


def _chunk_kind(c: Any) -> str:
    if isinstance(c, dict):
        ctx = c.get('_context')
        if isinstance(ctx, dict) and ctx.get('type') == 'reasoning':
            return 'reasoning'
    return 'answer'


def _frame_to_events(obj: dict[str, Any], state: dict[str, Any],
                     chunk_kind: dict[int, str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    # Error / rate-limit frame (tag 6 in the wire stream): a bare object with
    # httpCode/category/message and no `type`. Surface it instead of letting
    # the stream appear to end silently.
    if 'type' not in obj and (obj.get('httpCode') or obj.get('category') or obj.get('internalCode')):
        msg = str(obj.get('message') or obj.get('category') or 'unknown error')
        retry = obj.get('retryAfterSeconds')
        if retry:
            msg += ' (retry after ' + str(retry) + 's)'
        state['error'] = msg
        ctx = state.get('ctx')
        if ctx is not None:
            return [ctx.error(msg, http_code=obj.get('httpCode'), category=obj.get('category'))]
        return [{'type': 'error', 'message': msg}]
    if obj.get('type') == 'bootstrap':
        ch = obj.get('chat')
        if isinstance(ch, dict) and ch.get('id'):
            state['chat_id'] = ch['id']
        return events
    if obj.get('type') != 'message':
        return events

    mid = obj.get('messageId')
    if mid:
        state['assistant_id'] = mid

    for p in obj.get('patches', []) or []:
        op = p.get('op')
        path = p.get('path', '')
        val = p.get('value')
        if path == '/' and op in ('replace', 'add') and isinstance(val, dict):
            if val.get('id'):
                state['assistant_id'] = val['id']
            if val.get('messageVersion') is not None:
                state['assistant_version'] = val.get('messageVersion')
            if val.get('chatId'):
                state['chat_id'] = val['chatId']
            continue
        if path == '/generationStatus' and isinstance(val, str):
            state['gen_status'] = val
            continue
        if path == '/contentChunks' and op in ('replace', 'add') and isinstance(val, list):
            for i, c in enumerate(val):
                kind = _chunk_kind(c)
                chunk_kind[i] = kind
                txt = c.get('text') if isinstance(c, dict) else None
                if txt:
                    events.append({'type': kind, 'text': txt})
            continue
        if path.startswith('/contentChunks/') and path.count('/') == 2 and op in ('add', 'replace'):
            try:
                idx = int(path.split('/')[2])
            except ValueError:
                continue
            if isinstance(val, dict):
                kind = _chunk_kind(val)
                chunk_kind[idx] = kind
                if val.get('text'):
                    events.append({'type': kind, 'text': val['text']})
            continue
        if path.endswith('/_context') and op == 'replace':
            parts = path.split('/')
            if len(parts) >= 3:
                try:
                    idx = int(parts[2])
                except ValueError:
                    continue
                if isinstance(val, dict) and val.get('type') == 'reasoning':
                    chunk_kind[idx] = 'reasoning'
            continue
        if path.startswith('/contentChunks/') and path.endswith('/text') and op == 'append':
            parts = path.split('/')
            if len(parts) >= 3:
                try:
                    idx = int(parts[2])
                except ValueError:
                    continue
                kind = chunk_kind.get(idx, 'answer')
                if isinstance(val, str) and val:
                    events.append({'type': kind, 'text': val})
            continue

    return events


def _parse_trpc_body(text: str) -> Any:
    """Parse a tRPC response body into a flat list of payloads.

    ``?batch=1`` POSTs (file.uploadFile, file.confirmUpload, ...) return a JSON
    array of ``{"result":{"data":{"json": <payload>}}}`` envelopes -- NOT the
    ``<tag>:<json>`` SSE frames used by /api/chat. Handle both so batch calls
    actually expose their payload instead of collapsing to [].
    """
    text = text.strip()
    if not text:
        return []
    if text[0] in '[{':
        try:
            data = json.loads(text)
        except Exception:
            data = None
        if data is not None:
            return _unwrap_trpc(data)
    out = []
    for line in text.split('\n'):
        line = line.strip()
        if not line:
            continue
        obj = _decode_frame(line)
        if obj is not None:
            out.append(obj)
    return out


def _unwrap_trpc(data: Any) -> list[Any]:
    """Flatten tRPC batch envelopes down to their ``data.json`` payloads."""
    items = data if isinstance(data, list) else [data]
    out: list[Any] = []
    for it in items:
        if isinstance(it, dict) and 'result' in it:
            res = it.get('result')
            d = res.get('data') if isinstance(res, dict) else None
            if isinstance(d, dict) and 'json' in d:
                out.append(d['json'])
            elif d is not None:
                out.append(d)
            else:
                out.append(res)
        else:
            out.append(it)
    return out


def _first_url(info: Any, *keys: str) -> str | None:
    """First URL string under any of ``keys`` (accepts a list or a scalar)."""
    for k in keys:
        v = _find_key(info, k)
        if isinstance(v, list):
            for item in v:
                if isinstance(item, str) and item:
                    return item
        elif isinstance(v, str) and v:
            return v
    return None


def _find_key(obj: Any, key: str) -> Any:
    """Recursively find the first truthy value for `key` in nested dict/list."""
    if isinstance(obj, dict):
        if key in obj and obj[key]:
            return obj[key]
        for v in obj.values():
            r = _find_key(v, key)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_key(v, key)
            if r:
                return r
    return None


_client: MistralWebClient | None = None


def get_client() -> MistralWebClient:
    """Return the global MistralWebClient singleton."""
    global _client
    if _client is None:
        _client = MistralWebClient()
    return _client
