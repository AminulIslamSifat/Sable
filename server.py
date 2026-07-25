"""Sable FastAPI server with persistence and SSE chat streaming."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from collections.abc import AsyncGenerator, Callable, Generator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from engine.config import MODELS
from engine.scraper import (
    get_settings as get_scraper_settings,
    list_engines as list_scraper_engines,
    scraper as scraper_service,
    update_settings as update_scraper_settings,
)
from engine.service import ChatService
from engine.skills import SkillParser, build_tool_feedback, list_skills

logger = logging.getLogger("sable")

# Live log buffer for /api/logs SSE endpoint
_log_buffer: asyncio.Queue[str] = asyncio.Queue(maxsize=500)


class SSELogHandler(logging.Handler):
    """Non-blocking handler that pushes formatted log records into _log_buffer."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            _log_buffer.put_nowait(msg)
        except asyncio.QueueFull:
            pass  # drop oldest if buffer is full (fire-and-forget)


_sse_handler = SSELogHandler()
_sse_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S"))
logging.getLogger().addHandler(_sse_handler)
logging.getLogger().setLevel(logging.DEBUG)

MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0  # seconds; exponential backoff: 1s, 2s, 4s

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
INDEX_FILE = WEB_DIR / "index.html"
DB_PATH = BASE_DIR / "sable.db"
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

SKILL_ROUND_WARN_THRESHOLD = 15  # log a warning after this many rounds; no hard cap

service = ChatService(user_data_dir=str(BASE_DIR / "browser-data"))


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


async def retry_async(
    coro_factory: Callable[[], Any],
    *,
    max_retries: int = MAX_RETRIES,
    base_delay: float = RETRY_BASE_DELAY,
    label: str = "operation",
) -> Any:
    """Retry an async callable up to *max_retries* times with exponential backoff.

    ``coro_factory`` must be a zero-argument callable that returns a fresh
    awaitable each time it is called (so retries actually re-execute the work).
    On final failure the last exception is re-raised.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return await coro_factory()
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.warning(
                    "%s failed (attempt %d/%d): %s — retrying in %.1fs",
                    label, attempt + 1, max_retries + 1, exc, delay,
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "%s failed permanently after %d attempts: %s",
                    label, max_retries + 1, exc,
                )
    raise last_exc  # type: ignore[misc]


async def retry_stream(
    stream_factory: Callable[[], AsyncGenerator[dict[str, Any], None]],
    *,
    max_retries: int = MAX_RETRIES,
    base_delay: float = RETRY_BASE_DELAY,
    label: str = "stream",
) -> AsyncGenerator[dict[str, Any], None]:
    """Yield events from an async generator, retrying the whole stream on error."""
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            async for event in stream_factory():
                yield event
            return  # completed successfully
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.warning(
                    "%s failed (attempt %d/%d): %s — retrying in %.1fs",
                    label, attempt + 1, max_retries + 1, exc, delay,
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "%s failed permanently after %d attempts: %s",
                    label, max_retries + 1, exc,
                )
    if last_exc:
        raise last_exc


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chats (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT 'New chat',
                parent_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                thinking TEXT,
                skill_events TEXT,
                parent_id TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(chat_id) REFERENCES chats(id)
            )
            """
        )
        # Migration: older DBs created before skill_events existed won't have
        # this column just from CREATE TABLE IF NOT EXISTS above (that only
        # fires on a brand-new file). Add it if missing.
        existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
        if "skill_events" not in existing_cols:
            conn.execute("ALTER TABLE messages ADD COLUMN skill_events TEXT")


def ensure_chat(chat_id: str, title: str = "New chat", parent_id: str | None = None) -> None:
    now = utcnow()
    with get_db() as conn:
        existing = conn.execute("SELECT id FROM chats WHERE id = ?", (chat_id,)).fetchone()
        if existing:
            return
        conn.execute(
            "INSERT INTO chats (id, title, parent_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (chat_id, title, parent_id, now, now),
        )


def set_title_if_default(chat_id: str, title: str) -> None:
    with get_db() as conn:
        row = conn.execute("SELECT title FROM chats WHERE id = ?", (chat_id,)).fetchone()
        if row and row["title"] in ("New chat", ""):
            conn.execute("UPDATE chats SET title = ? WHERE id = ?", (title, chat_id))


def touch_chat(chat_id: str, parent_id: str | None = None) -> None:
    now = utcnow()
    with get_db() as conn:
        if parent_id is None:
            conn.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (now, chat_id))
        else:
            conn.execute(
                "UPDATE chats SET updated_at = ?, parent_id = ? WHERE id = ?",
                (now, parent_id, chat_id),
            )


def add_message(
    chat_id: str,
    role: str,
    content: str,
    thinking: str | None = None,
    parent_id: str | None = None,
    skill_events: list[dict[str, Any]] | None = None,
) -> int:
    now = utcnow()
    skill_events_json = json.dumps(skill_events, ensure_ascii=False) if skill_events else None
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO messages (chat_id, role, content, thinking, skill_events, parent_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, role, content, thinking, skill_events_json, parent_id, now),
        )
        return int(cur.lastrowid)


def get_messages(chat_id: str) -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, chat_id, role, content, thinking, skill_events, parent_id, created_at "
            "FROM messages WHERE chat_id = ? ORDER BY id ASC",
            (chat_id,),
        ).fetchall()
        messages = []
        for row in rows:
            msg = dict(row)
            raw_events = msg.get("skill_events")
            try:
                msg["skill_events"] = json.loads(raw_events) if raw_events else []
            except json.JSONDecodeError:
                msg["skill_events"] = []
            messages.append(msg)
        return messages


def list_chats() -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, title, parent_id, created_at, updated_at FROM chats ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]


def delete_chat(chat_id: str) -> bool:
    with get_db() as conn:
        conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        cur = conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
        return cur.rowcount > 0


@asynccontextmanager
async def lifespan(app: FastAPI) -> Generator[None, None, None]:
    init_db()
    await service.warmup()
    yield
    await service.close()
    await scraper_service.stop(kill_browser=True)


app = FastAPI(title="Sable API", version="0.4.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

if UPLOAD_DIR.exists():
    app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")


class ChatRequest(BaseModel):
    message: str
    chat_id: str | None = None
    parent_id: str | None = None
    files: list[dict[str, Any]] | None = None
    model: str | None = None
    thinking_mode: str | None = None
    stream: bool = True


class NewChatRequest(BaseModel):
    model: str | None = None


def sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def make_title(message: str) -> str:
    clean = " ".join(message.split())
    return clean[:48] or "New chat"


def get_parent_id(chat_id: str, requested_parent_id: str | None) -> str | None:
    if requested_parent_id:
        return requested_parent_id
    with get_db() as conn:
        row = conn.execute("SELECT parent_id FROM chats WHERE id = ?", (chat_id,)).fetchone()
        return row["parent_id"] if row else None


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/logs")
async def stream_logs():
    """SSE endpoint that streams live server logs to the frontend."""
    async def generator():
        while True:
            try:
                msg = await asyncio.wait_for(_log_buffer.get(), timeout=15.0)
                yield sse({"type": "log", "message": msg})
            except asyncio.TimeoutError:
                yield sse({"type": "ping"})
    return StreamingResponse(generator(), media_type="text/event-stream")


@app.get("/api/chats")
def chats() -> dict[str, list[dict[str, Any]]]:
    return {"chats": list_chats()}


DEEPSEEK_MODELS = [
    {"id": "default", "label": "Instant", "thinking_modes": []},
    {"id": "expert", "label": "Expert", "thinking_modes": []},
    {"id": "vision", "label": "Vision", "thinking_modes": []},
]


@app.get("/api/models")
def models() -> dict[str, list[dict[str, Any]]]:
    # When scraper is active with DeepSeek, show DS model types instead of Qwen
    scraper_cfg = get_scraper_settings()
    if scraper_cfg.get("enabled") and scraper_cfg.get("engine_type") == "deepseek":
        return {"models": DEEPSEEK_MODELS}
    return {
        "models": [
            {
                "id": m["id"],
                "label": m["label"],
                "thinking_modes": [
                    {"id": tm["id"], "label": tm["label"]} for tm in m["thinking_modes"]
                ],
            }
            for m in MODELS
        ]
    }


@app.post("/api/chat/new")
async def new_chat(request: NewChatRequest = NewChatRequest()) -> dict[str, str | None]:
    if get_scraper_settings().get("enabled"):
        chat_id = f"browser-{uuid.uuid4().hex}"
        ensure_chat(chat_id, "New chat", None)
        return {"chat_id": chat_id}

    try:
        chat_id = await retry_async(
            lambda: service.create_chat(model=request.model),
            label="create_chat",
        )
    except Exception as exc:
        return {"error": f"Session startup failed: {type(exc).__name__}: {exc}"}
    if not chat_id:
        return {"error": "Could not create chat session"}
    ensure_chat(chat_id, "New chat", None)
    return {"chat_id": chat_id}


@app.get("/api/chats/{chat_id}/messages")
def chat_messages(chat_id: str) -> dict[str, Any]:
    return {"chat_id": chat_id, "messages": get_messages(chat_id)}


@app.delete("/api/chats/{chat_id}")
def delete_chat_route(chat_id: str) -> dict[str, Any]:
    deleted = delete_chat(chat_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Chat not found")
    return {"deleted": True, "chat_id": chat_id}


@app.get("/api/skills")
def skills() -> dict[str, list[dict[str, Any]]]:
    return {"skills": list_skills()}


@app.post("/api/sync-context")
async def sync_context_route() -> dict[str, Any]:
    success = await service.sync_context()
    if success:
        return {"status": "ok", "message": "Context synced successfully"}
    raise HTTPException(status_code=500, detail="Failed to sync context")


@app.get("/api/settings/scraper")
async def get_scraper_settings_route() -> dict[str, Any]:
    return get_scraper_settings()


@app.get("/api/settings/scraper/engines")
async def get_scraper_engines_route() -> dict[str, Any]:
    return {"engines": list_scraper_engines()}


@app.post("/api/settings/scraper")
async def update_scraper_settings_route(payload: dict[str, Any]) -> dict[str, Any]:
    settings = update_scraper_settings(payload)
    # Force the engine to reload with new settings on the next chat request.
    await scraper_service.stop()

    # Pre-launch browser immediately when scraper is enabled.
    if settings.get("enabled"):
        prelaunch_result = await scraper_service.prelaunch()
        settings["prelaunch"] = prelaunch_result

    return settings


@app.post("/api/scraper/model")
async def switch_scraper_model(payload: dict[str, Any]) -> dict[str, Any]:
    """Switch the browser engine's active model type (DeepSeek Instant/Expert/Vision)."""
    model_type = str(payload.get("model_type") or "default").strip()
    return await scraper_service.switch_model(model_type)


@app.get("/api/settings/browser")
async def get_browser_settings() -> dict[str, bool]:
    return {"headless": service.browser_headless}


@app.post("/api/settings/browser")
async def update_browser_settings(payload: dict[str, bool]) -> dict[str, Any]:
    headless = payload.get("headless")
    if headless is None:
        raise HTTPException(status_code=400, detail="Missing 'headless' field")
    await service.restart_browser(headless=headless)
    return {"status": "ok", "headless": service.browser_headless}


_MEMORY_PATH = Path(__file__).resolve().parent / "Brain" / "Memory.json"


@app.get("/api/settings/memory")
async def get_memory() -> dict[str, Any]:
    if not _MEMORY_PATH.exists():
        return {"memory": {"semantic": [], "episodic": [], "procedural": []}}
    try:
        data = json.loads(_MEMORY_PATH.read_text(encoding="utf-8"))
        return {"memory": data}
    except Exception:
        return {"memory": {"semantic": [], "episodic": [], "procedural": []}}


@app.post("/api/settings/memory")
async def update_memory(payload: dict[str, Any]) -> dict[str, str]:
    memory = payload.get("memory")
    if memory is None:
        raise HTTPException(status_code=400, detail="Missing 'memory' field")
    _MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _MEMORY_PATH.write_text(json.dumps(memory, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"status": "ok"}


_CONSOLIDATE_PROMPT_TEMPLATE = (
    "[SYSTEM: Memory consolidation pass. You already have the full conversation above. Do NOT re-summarize it.]\n\n"
    "CURRENT MEMORY STORE:\n<<CURRENT_MEMORY>>\n\n"
    "TASK: Scan the conversation above. Extract ONLY facts that satisfy ALL of these:\n"
    "1. NOT already present in the current memory store\n"
    "2. NOT covered by persona or instruction files\n"
    "3. Genuinely important for future sessions (architecture decisions, user preferences, project milestones, key bugs found)\n"
    "4. Information-dense: specific names, numbers, decisions, paths — never vague summaries\n\n"
    "STRICT RULES:\n"
    "- DEFAULT TO EMPTY. Most conversations produce zero new memories. That is correct.\n"
    "- Routine debugging, casual chat, transient tasks, greetings = NO entries.\n"
    "- Only add an entry if forgetting it would cause real problems in the next session.\n"
    "- Maximum 3 entries total unless the conversation was truly groundbreaking.\n\n"
    'OUTPUT: Raw JSON only, no markdown fences, no explanation.\n'
    'Format: {"semantic": [], "episodic": [], "procedural": []}\n'
    'Each entry: {"key": "short_label", "value": "dense specific fact"}\n'
    'If nothing qualifies, return exactly: {"semantic": [], "episodic": [], "procedural": []}'
)


@app.post("/api/memory/consolidate")
async def consolidate_memory(payload: dict[str, Any]) -> dict[str, Any]:
    chat_id = payload.get("chat_id")
    model = payload.get("model")
    if not chat_id:
        raise HTTPException(status_code=400, detail="Missing 'chat_id'")

    messages = get_messages(chat_id)
    if len(messages) < 2:
        return {"status": "skipped", "reason": "too few messages"}

    # Load current memory
    current_memory = "{}"
    if _MEMORY_PATH.exists():
        try:
            current_memory = _MEMORY_PATH.read_text(encoding="utf-8")
        except Exception:
            current_memory = "{}"

    prompt = _CONSOLIDATE_PROMPT_TEMPLATE.replace("<<CURRENT_MEMORY>>", current_memory)

    # Send consolidation prompt in the SAME chat — model already has full context
    try:
        result = await retry_async(
            lambda: service.chat(
                message=prompt,
                chat_id=chat_id,
                model=model,
            ),
            label="memory_consolidate",
        )
        raw_answer = str(result.get("answer", ""))
    except Exception as exc:
        return {"status": "error", "detail": f"Model call failed: {exc}"}

    # Parse JSON from response (strip markdown fences, extract JSON object)
    cleaned = raw_answer.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()

    # Fallback: extract first {...} block if direct parse fails
    try:
        new_entries = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end > start:
            try:
                new_entries = json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                return {"status": "error", "detail": "Model returned invalid JSON", "raw": raw_answer[:500]}
        else:
            return {"status": "error", "detail": "No JSON object found in response", "raw": raw_answer[:500]}

    if not isinstance(new_entries, dict):
        return {"status": "error", "detail": "Expected dict with semantic/episodic/procedural keys"}

    # Merge into existing memory (deduplicate by key)
    existing: dict[str, list[dict[str, str]]] = {}
    if _MEMORY_PATH.exists():
        try:
            existing = json.loads(_MEMORY_PATH.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except Exception:
            existing = {}

    added_count = 0
    for cat in ("semantic", "episodic", "procedural"):
        existing_list = existing.get(cat, [])
        new_list = new_entries.get(cat, [])
        if not isinstance(new_list, list):
            continue
        existing_keys = {e.get("key", "") for e in existing_list if isinstance(e, dict)}
        for entry in new_list:
            if isinstance(entry, dict) and entry.get("key") and entry["key"] not in existing_keys:
                existing_list.append({"key": entry["key"], "value": entry.get("value", "")})
                existing_keys.add(entry["key"])
                added_count += 1
        existing[cat] = existing_list

    _MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _MEMORY_PATH.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")

    return {"status": "ok", "added": added_count}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)) -> dict[str, Any]:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")

    suffix = Path(file.filename).suffix or ".bin"
    stored_name = f"{uuid.uuid4().hex}{suffix}"
    target = UPLOAD_DIR / stored_name

    raw = await file.read()
    target.write_bytes(raw)

    result = await service.upload_image(str(target))
    if result is None:
        return {"uploaded": False, "path": str(target)}

    return {"uploaded": True, "path": str(target), "meta": result}


@app.post("/api/chat")
async def chat(request: ChatRequest):
    scraper_enabled = get_scraper_settings().get("enabled")

    active_chat_id = request.chat_id
    if not active_chat_id and scraper_enabled:
        active_chat_id = f"browser-{uuid.uuid4().hex}"

    if not active_chat_id:
        try:
            active_chat_id = await retry_async(
                lambda: service.create_chat(model=request.model),
                label="create_chat",
            )
        except Exception as exc:
            return {"error": f"Session startup failed: {type(exc).__name__}: {exc}"}
        if not active_chat_id:
            return {"error": "Could not create chat session"}

    timestamped_message = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]\n{request.message}"

    title = make_title(request.message)
    ensure_chat(active_chat_id, title, request.parent_id)
    set_title_if_default(active_chat_id, title)

    parent_id = get_parent_id(active_chat_id, request.parent_id)
    add_message(active_chat_id, "user", timestamped_message, None, parent_id)

    # Resolve file entries: if only a path is given, upload to get full file object
    resolved_files: list[dict[str, Any]] | None = None
    if request.files:
        resolved_files = []
        for f in request.files:
            if scraper_enabled:
                if "path" in f or "url" in f:
                    resolved_files.append(f)
                continue
            if "id" in f and "url" in f:
                resolved_files.append(f)  # already a full file object
            elif "path" in f:
                meta = await service.upload_image(f["path"])
                if meta:
                    resolved_files.append(meta)
                else:
                    print(f"[WARN] Could not resolve file: {f['path']}")

    if not request.stream and scraper_enabled:
        result = await scraper_service.chat(
            message=timestamped_message,
            chat_id=active_chat_id,
            parent_id=parent_id,
            files=resolved_files,
            model=request.model,
            thinking_mode=request.thinking_mode,
        )
        answer = str(result.get("answer", ""))
        thinking = str(result.get("thinking", ""))
        final_parent = result.get("parent_id") or parent_id
        error = result.get("error")

        add_message(active_chat_id, "assistant", answer or error or "", thinking, final_parent)
        touch_chat(active_chat_id, final_parent)
        return result

    if not request.stream and not scraper_enabled:
        result = await retry_async(
            lambda: service.chat(
                message=timestamped_message,
                chat_id=active_chat_id,
                parent_id=parent_id,
                files=resolved_files,
                model=request.model,
                thinking_mode=request.thinking_mode,
            ),
            label="chat",
        )
        answer = str(result.get("answer", ""))
        thinking = str(result.get("thinking", ""))
        final_parent = result.get("parent_id") or parent_id
        error = result.get("error")

        add_message(active_chat_id, "assistant", answer or error or "", thinking, final_parent)
        touch_chat(active_chat_id, final_parent)
        return result

    async def event_stream():
        answer_parts: list[str] = []
        thinking_parts: list[str] = []
        skill_events: list[dict[str, Any]] = []
        final_parent = parent_id
        error_message: str | None = None
        current_message = timestamped_message
        current_parent = parent_id
        round_index = 0

        yield sse({"type": "status", "message": "processing"})

        try:
            while True:
                round_skill_events: list[dict[str, Any]] = []
                round_thinking_parts: list[str] = []
                parser = SkillParser()

                # These stay plain (sync) generators — they don't await anything,
                # they just re-shape parser output. Iterating them with a plain
                # `for` loop and re-yielding works fine inside an async def;
                # `yield from` itself is a SyntaxError inside async functions,
                # which is why every call site below uses an explicit loop.
                def emit_parsed(text: str) -> Generator[str, None, None]:
                    for item in parser.feed(text):
                        if item.get("type") == "text":
                            chunk = str(item.get("text", ""))
                            if chunk:
                                answer_parts.append(chunk)
                                yield sse({"type": "answer", "text": chunk})
                        else:
                            if item.get("type") in ("skill_start", "skill_output", "skill_end", "file_edit"):
                                round_skill_events.append(item)
                            yield sse(item)

                def emit_flush() -> Generator[str, None, None]:
                    for item in parser.flush():
                        if item.get("type") == "text":
                            chunk = str(item.get("text", ""))
                            if chunk:
                                answer_parts.append(chunk)
                                yield sse({"type": "answer", "text": chunk})
                        else:
                            if item.get("type") in ("skill_start", "skill_output", "skill_end", "file_edit"):
                                round_skill_events.append(item)
                            yield sse(item)

                files_for_round = resolved_files if round_index == 0 else None
                stream_error = False

                if scraper_enabled:
                    round_event_source = scraper_service.stream_events(
                        message=current_message,
                        chat_id=active_chat_id,
                        parent_id=current_parent,
                        files=files_for_round,
                        model=request.model,
                        thinking_mode=request.thinking_mode,
                    )
                else:
                    round_event_source = retry_stream(
                        lambda: service.stream_events(
                            message=current_message,
                            chat_id=active_chat_id,
                            parent_id=current_parent,
                            files=files_for_round,
                            model=request.model,
                            thinking_mode=request.thinking_mode,
                        ),
                        label=f"stream_round_{round_index}",
                    )

                async for event in round_event_source:
                    event_type = event.get("type")

                    if event_type == "answer":
                        for _sse_line in emit_parsed(str(event.get("text", ""))):
                            yield _sse_line
                        continue

                    if event_type == "thinking":
                        thinking_parts.append(str(event.get("text", "")))
                        round_thinking_parts.append(str(event.get("text", "")))
                    elif event_type == "done":
                        for _sse_line in emit_flush():
                            yield _sse_line
                        final_parent = event.get("parent_id") or final_parent
                        current_parent = final_parent
                    elif event_type == "error":
                        for _sse_line in emit_flush():
                            yield _sse_line
                        error_message = str(event.get("message", "Unknown error"))
                        stream_error = True
                    elif event_type == "rate_limited":
                        for _sse_line in emit_flush():
                            yield _sse_line
                        hours = event.get("hours", "?")
                        details = event.get("message", "Daily usage limit reached.")
                        error_message = f"⏳ Rate Limited — {details} (retry in {hours}h)"
                        stream_error = True

                    yield sse(event)

                # Preserve per-round ordering (thinking -> that round's commands)
                # so the history loader can rebuild the t1,c1,t2,c2 layout.
                round_thinking_text = "".join(round_thinking_parts)
                if round_thinking_text:
                    skill_events.append({"type": "round_thinking", "text": round_thinking_text})
                if round_skill_events:
                    skill_events.extend(round_skill_events)

                feedback = build_tool_feedback(round_skill_events)

                if stream_error or error_message or not feedback:
                    break

                if round_index >= SKILL_ROUND_WARN_THRESHOLD:
                    logger.warning(
                        "chat %s reached %d skill rounds — still running but worth checking",
                        active_chat_id, round_index,
                    )
                    yield sse({"type": "status", "message": "high_skill_round_count", "round": round_index})

                round_index += 1
                current_message = feedback
                current_parent = final_parent
                yield sse(
                    {
                        "type": "status",
                        "message": "feeding_skill_results",
                        "round": round_index,
                    }
                )
        except Exception as exc:
            error_message = f"{type(exc).__name__}: {exc}"
            try:
                yield sse({"type": "error", "message": f"Server error: {error_message}"})
            except Exception:
                pass
        finally:
            answer = "".join(answer_parts)
            thinking = "".join(thinking_parts)

            if not answer and skill_events:
                summary = []
                for evt in skill_events:
                    if evt.get("type") == "skill_start":
                        summary.append(f"[skill] {evt.get('name', 'skill')}")
                    elif evt.get("type") == "skill_end":
                        status = "ok" if evt.get("ok") else "error"
                        summary.append(f"[{status}] {evt.get('name', 'skill')}")
                answer = "\n".join(summary)

            stored_content = answer or error_message or ""
            add_message(active_chat_id, "assistant", stored_content, thinking, final_parent, skill_events)
            touch_chat(active_chat_id, final_parent)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    if INDEX_FILE.exists():
        return INDEX_FILE.read_text(encoding="utf-8")
    return "<h1>Sable API is running</h1><p>POST /api/chat</p>"


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="127.0.0.1", port=6000, reload=False)