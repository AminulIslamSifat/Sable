"""
Sable Telegram Bot — Full-feature chat surface with VSCode extension parity.

Talks to Sable server ONLY via existing HTTP endpoints:
  POST /api/chat              — send message, receive SSE stream
  POST /api/chat/new          — create new chat session
  POST /api/skills/approve/:id — approve permission request
  POST /api/skills/deny/:id   — deny permission request
  GET  /api/chats             — list chats
  GET  /api/models            — list available models

Handles ALL SSE event types:
  - answer/token, thinking, done, error, status
  - skill_start, skill_output, skill_end
  - ask_user → inline keyboard with options
  - permission_request → approve/session/deny buttons
  - approval_pending, sim_ready, chat_title
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import sys
from pathlib import Path
from typing import Any

from telegram import (
    Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logger = logging.getLogger("sable_tg_bot")

# ── Paths ────────────────────────────────────────────────────────────────────
_SYSTEM_DIR = Path(__file__).resolve().parent.parent / "system"
_CONFIG_PATH = _SYSTEM_DIR / ".telegram_bot_config.json"
_SESSIONS_PATH = _SYSTEM_DIR / ".telegram_bot_sessions.json"
_AUTH_TOKEN_PATH = _SYSTEM_DIR / ".auth_token"

# ── Defaults ─────────────────────────────────────────────────────────────────
_MAX_TG_MESSAGE_LEN = 4096
_STREAM_TIMEOUT = 300  # 5 min max wait for agent response
_TG_TOOL_OUTPUT_MAX = 800  # Max chars per tool output in TG summary
_STOP_CALLBACK_PREFIX = "stop_stream:"  # Callback data prefix for stop button

# ── Panel callback prefixes (must be short — TG has 64-byte limit) ─────────
_CB_PANEL_MODEL = "p_mod"        # Open model selector panel
_CB_PANEL_PERSONA = "p_per"      # Open persona selector panel
_CB_PANEL_THINKING = "p_thk"     # Toggle thinking mode
_CB_PANEL_RESEARCH = "p_res"     # Open research panel
_CB_PANEL_NOTES = "p_not"        # Open notes panel
_CB_PANEL_GALLERY = "p_gal"      # Open gallery panel
_CB_PANEL_HISTORY = "p_his"      # Open chat history panel
_CB_PANEL_MEDIA = "p_med"        # Media info
_CB_SET_MODEL = "s_mod:"         # Set model: s_mod:<model_id>
_CB_SET_PERSONA = "s_per:"       # Set persona: s_per:<name>
_CB_SET_THINKING = "s_thk:"      # Set thinking mode: s_thk:<mode_id>
_CB_SWITCH_CHAT = "s_cht:"       # Switch chat: s_cht:<chat_id>
_CB_PAGE = "pg:"                 # Pagination: pg:<panel>:<page>
_CB_FILE_GALLERY = "f_gal:"      # Send gallery file: f_gal:<filename>
_CB_FILE_NOTE = "f_not:"         # Send note file: f_not:<filename>
_CB_FILE_RESEARCH = "f_res:"     # Send research file: f_res:<filename>

# Persistent keyboard button labels
_KB_MODEL = "🤖 Model"
_KB_PERSONA = "🎭 Persona"
_KB_THINKING = "🧠 Thinking"
_KB_RESEARCH = "🔬 Research"
_KB_NOTES = "📝 Notes"
_KB_GALLERY = "🖼 Gallery"
_KB_HISTORY = "💬 History"
_KB_MEDIA = "📎 Media"


# ── Config helpers ───────────────────────────────────────────────────────────
def load_config() -> dict[str, Any]:
    if not _CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg: dict[str, Any]) -> None:
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def get_server_url() -> str:
    """Auto-detect Sable server URL from engine config (same process)."""
    try:
        from engine.config import PORT, HOST
        host = "localhost" if HOST in ("0.0.0.0", "::") else HOST
        return f"http://{host}:{PORT}"
    except ImportError:
        # Fallback: try reading from env or config
        import os
        port = os.getenv("SABLE_PORT", os.getenv("PORT", "61770"))
        return f"http://localhost:{port}"


def get_auth_token() -> str:
    """Load Sable auth token from system folder."""
    if _AUTH_TOKEN_PATH.exists():
        return _AUTH_TOKEN_PATH.read_text(encoding="utf-8").strip()
    return ""


def get_allowed_users() -> list[int]:
    """Return list of allowed Telegram user IDs. Empty = allow all."""
    cfg = load_config()
    raw = cfg.get("allowed_users", [])
    if isinstance(raw, str):
        return [int(x.strip()) for x in raw.split(",") if x.strip().isdigit()]
    return [int(x) for x in raw if str(x).isdigit()]


def is_user_allowed(user_id: int) -> bool:
    allowed = get_allowed_users()
    return not allowed or user_id in allowed


# ── Session mapping ──────────────────────────────────────────────────────────
def _load_sessions() -> dict[str, str]:
    if not _SESSIONS_PATH.exists():
        return {}
    try:
        return json.loads(_SESSIONS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_sessions(sessions: dict[str, str]) -> None:
    _SESSIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SESSIONS_PATH.write_text(json.dumps(sessions, indent=2), encoding="utf-8")


def get_chat_id_for_user(tg_user_id: int) -> str | None:
    sessions = _load_sessions()
    return sessions.get(str(tg_user_id))


def set_chat_id_for_user(tg_user_id: int, chat_id: str) -> None:
    sessions = _load_sessions()
    sessions[str(tg_user_id)] = chat_id
    _save_sessions(sessions)


def clear_chat_for_user(tg_user_id: int) -> None:
    sessions = _load_sessions()
    sessions.pop(str(tg_user_id), None)
    _save_sessions(sessions)


# ── Active stream tracking (for stop button) ────────────────────────────────
_active_streams: dict[int, asyncio.Task] = {}  # tg_user_id → running stream task


def _truncate_tool_output(text: str, max_chars: int = _TG_TOOL_OUTPUT_MAX) -> str:
    """Middle-truncate tool output for TG display, preserving head + tail."""
    if len(text) <= max_chars:
        return text
    head = max_chars // 3
    tail = max_chars - head - 20  # room for truncation marker
    return text[:head] + f"\n… [{len(text)} chars truncated] …\n" + text[-tail:]


def _extract_image_paths(result: dict | None) -> list[str]:
    """Extract local image file paths from a skill_end result dict.

    Handles both single-image (result.path) and multi-image (result.images[].path)
    formats produced by generate_image handler.
    """
    if not result or not isinstance(result, dict):
        return []
    paths: list[str] = []
    # Multi-image: result.images is a list of dicts with 'path' keys
    images = result.get("images")
    if isinstance(images, list):
        for img in images:
            if isinstance(img, dict):
                p = img.get("path", "")
                if p and Path(p).is_file():
                    paths.append(p)
    # Single/fallback: result.path
    single = result.get("path", "")
    if single and Path(single).is_file() and single not in paths:
        paths.insert(0, single)
    return paths


async def _send_generated_images(
    message: Any,
    result: dict | None,
) -> None:
    """Send generated images as Telegram photos after a tool_end event.

    Reads local files directly and sends via reply_photo.
    Falls back to reply_document for non-photo extensions.
    """
    paths = _extract_image_paths(result)
    if not paths:
        return
    photo_exts = {".png", ".jpg", ".jpeg", ".webp"}
    for img_path in paths:
        try:
            p = Path(img_path)
            data = p.read_bytes()
            ext = p.suffix.lower()
            caption = f"🖼 `{p.name}`"
            if ext in photo_exts:
                await message.reply_photo(
                    photo=io.BytesIO(data),
                    caption=caption,
                    parse_mode="Markdown",
                )
            else:
                await message.reply_document(
                    document=io.BytesIO(data),
                    filename=p.name,
                    caption=caption,
                    parse_mode="Markdown",
                )
        except Exception as exc:
            logger.warning("Failed to send image %s: %s", img_path, exc)


# ── User preferences (model, persona, thinking mode per user) ───────────────
_PREFS_PATH = _SYSTEM_DIR / ".telegram_bot_prefs.json"


def _load_prefs() -> dict[str, Any]:
    if not _PREFS_PATH.exists():
        return {}
    try:
        return json.loads(_PREFS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_prefs(prefs: dict[str, Any]) -> None:
    _PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PREFS_PATH.write_text(json.dumps(prefs, indent=2), encoding="utf-8")


def get_user_pref(tg_user_id: int, key: str, default: Any = None) -> Any:
    """Get a user preference value. Keys: model, persona, thinking_mode."""
    prefs = _load_prefs()
    user_prefs = prefs.get(str(tg_user_id), {})
    return user_prefs.get(key, default)


def set_user_pref(tg_user_id: int, key: str, value: Any) -> None:
    """Set a user preference value."""
    prefs = _load_prefs()
    uid = str(tg_user_id)
    if uid not in prefs:
        prefs[uid] = {}
    prefs[uid][key] = value
    _save_prefs(prefs)


def get_all_user_prefs(tg_user_id: int) -> dict[str, Any]:
    """Get all preferences for a user."""
    prefs = _load_prefs()
    return prefs.get(str(tg_user_id), {})


# ── Persistent Reply Keyboard ────────────────────────────────────────────────
def _build_persistent_keyboard() -> ReplyKeyboardMarkup:
    """Build the persistent reply keyboard shown at bottom of chat."""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(_KB_MODEL), KeyboardButton(_KB_PERSONA), KeyboardButton(_KB_THINKING)],
            [KeyboardButton(_KB_RESEARCH), KeyboardButton(_KB_NOTES), KeyboardButton(_KB_GALLERY)],
            [KeyboardButton(_KB_HISTORY), KeyboardButton(_KB_MEDIA)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


# ── Sable API client ────────────────────────────────────────────────────────
async def _sable_request(
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
    timeout: float = 30,
) -> tuple[int, Any]:
    """Make an HTTP request to Sable server. Returns (status, parsed_json_or_text)."""
    import aiohttp

    url = f"{get_server_url()}{path}"
    headers = {"Authorization": f"Bearer {get_auth_token()}"}
    if json_body is not None:
        headers["Content-Type"] = "application/json"

    async with aiohttp.ClientSession() as session:
        async with session.request(
            method, url, json=json_body, headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            status = resp.status
            try:
                body = await resp.json()
            except Exception:
                body = await resp.text()
            return status, body


async def _sable_download(path: str, *, timeout: float = 30) -> tuple[int, bytes]:
    """Download binary content from Sable server. Returns (status, raw_bytes)."""
    import aiohttp

    url = f"{get_server_url()}{path}"
    headers = {"Authorization": f"Bearer {get_auth_token()}"}

    async with aiohttp.ClientSession() as session:
        async with session.get(
            url, headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            status = resp.status
            data = await resp.read()
            return status, data


async def create_new_chat(model: str | None = None) -> str | None:
    """Create a new Sable chat session. Returns chat_id or None."""
    payload: dict[str, Any] = {}
    if model:
        payload["model"] = model
    status, body = await _sable_request("POST", "/api/chat/new", json_body=payload)
    if status == 200 and isinstance(body, dict):
        return body.get("chat_id")
    logger.error("Failed to create chat: %s %s", status, body)
    return None


# ── SSE Stream Result ────────────────────────────────────────────────────────
class StreamResult:
    """Result from consuming an SSE stream — carries response + interactive events."""
    __slots__ = ("answer", "thinking", "ask_user_payload", "permission_request",
                 "error", "skills", "paused", "tool_activities")

    def __init__(self) -> None:
        self.answer: str = ""
        self.thinking: str = ""
        self.ask_user_payload: dict | None = None
        self.permission_request: dict | None = None
        self.error: str | None = None
        self.skills: list[dict] = []
        self.paused: bool = False  # True when ask_user or permission_request stops the stream
        self.tool_activities: list[dict[str, Any]] = []  # [{name, output, ok, duration_ms}]


class StreamDisplay:
    """Manages a single Telegram message that streams thinking + answer tokens.

    Throttles edits to avoid Telegram rate limits (~1 edit/sec).
    Formats: 💭 thinking (italic) followed by answer text.
    """

    MIN_EDIT_INTERVAL = 0.8  # seconds between edits

    def __init__(self, message: Any, stop_kb: InlineKeyboardMarkup | None = None, thinking_enabled: bool = True):
        self._message = message
        self._stop_kb = stop_kb
        self._thinking_enabled = thinking_enabled
        self._thinking = ""
        self._answer = ""
        self._last_edit_time: float = 0
        self._msg: Any = None  # The live status message being edited
        self._dirty = False
        self._finalized = False

    async def start(self) -> None:
        """Create the initial streaming message."""
        if self._thinking_enabled:
            init_text = "💭 _Thinking..._"
            fallback = "💭 Thinking..."
        else:
            init_text = "⏳ _Processing..._"
            fallback = "⏳ Processing..."
        try:
            self._msg = await self._message.reply_text(init_text, parse_mode="Markdown", reply_markup=self._stop_kb)
        except Exception:
            self._msg = await self._message.reply_text(fallback, reply_markup=self._stop_kb)

    async def append_thinking(self, text: str) -> None:
        if not text or self._finalized:
            return
        self._thinking += text
        self._dirty = True
        await self._maybe_edit()

    async def append_answer(self, text: str) -> None:
        if not text or self._finalized:
            return
        self._answer += text
        self._dirty = True
        await self._maybe_edit()

    async def _maybe_edit(self) -> None:
        import time
        now = time.monotonic()
        if now - self._last_edit_time < self.MIN_EDIT_INTERVAL:
            return
        if not self._dirty or not self._msg:
            return
        self._dirty = False
        self._last_edit_time = now
        display = self._build_display()
        try:
            await self._msg.edit_text(display, parse_mode="Markdown", reply_markup=self._stop_kb)
        except Exception:
            pass

    def _build_display(self) -> str:
        parts: list[str] = []
        if self._thinking and self._thinking_enabled:
            # Truncate long thinking for display
            t = self._thinking
            if len(t) > 2000:
                t = t[:2000] + "…"
            parts.append(f"💭 _{t}_")
        if self._answer:
            parts.append(self._answer)
        if not parts:
            if self._thinking_enabled:
                return "💭 _Thinking..._"
            return "⏳ _Processing..._"
        return "\n\n".join(parts)

    async def finalize(self) -> None:
        """Flush any pending edits and mark as done."""
        self._finalized = True
        if self._dirty and self._msg:
            display = self._build_display()
            try:
                await self._msg.edit_text(display, parse_mode="Markdown", reply_markup=None)
            except Exception:
                pass

    @property
    def msg(self) -> Any:
        return self._msg


async def stream_chat_events(
    chat_id: str,
    message: str,
    *,
    model: str | None = None,
    thinking_mode: str | None = None,
    on_status: Any = None,
    on_tool_event: Any = None,
    on_thinking: Any = None,
    on_answer: Any = None,
) -> StreamResult:
    """Send message to Sable and consume SSE stream with full event handling.

    Args:
        chat_id: Sable chat session ID.
        message: User message text.
        model: Optional model override.
        on_status: Async callback(status_text: str) for live status updates.
        on_tool_event: Async callback(event_type: str, data: dict) for tool events.
            Called with "start" or "end" and the event payload.
        on_thinking: Async callback(text: str) called per thinking token.
        on_answer: Async callback(text: str) called per answer/token chunk.

    Returns:
        StreamResult with accumulated response and any interactive events.
    """
    import aiohttp

    url = f"{get_server_url()}/api/chat"
    headers = {
        "Authorization": f"Bearer {get_auth_token()}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "message": message,
        "chat_id": chat_id,
        "stream": True,
    }
    if model:
        payload["model"] = model
    if thinking_mode:
        payload["thinking_mode"] = thinking_mode

    logger.info("STREAM → POST %s chat_id=%s msg=%r", url, chat_id, message[:80])
    result = StreamResult()
    answer_parts: list[str] = []
    thinking_parts: list[str] = []
    current_skill_name: str | None = None
    current_tool_output_parts: list[str] = []  # Accumulate output for current tool

    async with aiohttp.ClientSession() as session:
        async with session.post(
            url, json=payload, headers=headers,
            timeout=aiohttp.ClientTimeout(total=_STREAM_TIMEOUT),
        ) as resp:
            logger.info("STREAM ← status=%d", resp.status)
            if resp.status != 200:
                err = await resp.text()
                logger.error("STREAM ERROR: %s", err[:200])
                result.error = f"Server error ({resp.status}): {err[:500]}"
                return result

            # Detect non-SSE JSON error responses (e.g. provider/model lock)
            content_type = resp.headers.get("content-type", "")
            if "text/event-stream" not in content_type:
                raw = await resp.text()
                logger.warning("Non-SSE response (content-type=%s): %s", content_type, raw[:300])
                try:
                    body = json.loads(raw)
                    if isinstance(body, dict) and body.get("error"):
                        result.error = body["error"]
                        return result
                except (json.JSONDecodeError, TypeError):
                    pass
                # Not JSON either — treat as empty/error
                if raw.strip():
                    result.error = f"Unexpected response format: {raw[:300]}"
                else:
                    result.error = "Empty response from server (possible model/provider lock). Try switching models or starting a new chat."
                return result

            buffer = ""
            async for chunk in resp.content.iter_chunked(1024):
                buffer += chunk.decode("utf-8", errors="replace")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line.startswith("data: "):
                        continue
                    try:
                        event = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue

                    etype = event.get("type", "")

                    if etype in ("answer", "token"):
                        token_text = event.get("text", "")
                        answer_parts.append(token_text)
                        if on_answer and token_text:
                            await on_answer(token_text)

                    elif etype == "thinking":
                        think_text = event.get("text", "")
                        thinking_parts.append(think_text)
                        if on_thinking and think_text:
                            await on_thinking(think_text)

                    elif etype == "skill_start":
                        name = event.get("name", "unknown")
                        current_skill_name = name
                        current_tool_output_parts = []  # Reset output accumulator
                        result.skills.append(event)
                        if on_tool_event:
                            await on_tool_event("start", event)

                    elif etype == "skill_output":
                        result.skills.append(event)
                        # ask_user: parse JSON payload for inline keyboard
                        if event.get("name") == "ask_user":
                            try:
                                result.ask_user_payload = json.loads(event.get("text", "{}"))
                            except (json.JSONDecodeError, TypeError):
                                pass
                        else:
                            # Accumulate raw output for tool activity summary
                            text = event.get("text", "")
                            if text:
                                current_tool_output_parts.append(text)

                    elif etype == "skill_end":
                        result.skills.append(event)
                        ok = event.get("ok", False)
                        name = event.get("name", "unknown")
                        duration_ms = event.get("duration_ms", 0)
                        # Finalize tool activity entry
                        raw_output = "".join(current_tool_output_parts)
                        result.tool_activities.append({
                            "name": name,
                            "output": raw_output,
                            "ok": ok,
                            "duration_ms": duration_ms,
                        })
                        current_tool_output_parts = []
                        # Send tool result as separate message
                        skill_result = event.get("result", {})
                        if on_tool_event:
                            await on_tool_event("end", {
                                "name": name,
                                "ok": ok,
                                "duration_ms": duration_ms,
                                "output": raw_output,
                                "result": skill_result,
                            })
                        # Check for ask_user pause
                        res = event.get("result", {})
                        if isinstance(res, dict) and res.get("pause"):
                            result.paused = True

                    elif etype == "permission_request":
                        result.permission_request = event
                        result.paused = True
                        if on_status:
                            cmd = event.get("data", {}).get("command", "")[:80]
                            await on_status(f"🔒 Permission needed: {cmd}")

                    elif etype == "approval_pending":
                        if on_status:
                            await on_status("⏳ Waiting for approval...")

                    elif etype == "status":
                        msg = event.get("message", "")
                        if on_status and msg:
                            # Clean up internal status identifiers for display
                            display_msg = msg.replace("_", " ").title() if msg.islower() or "_" in msg else msg
                            await on_status(f"📡 {display_msg}")

                    elif etype == "done":
                        break

                    elif etype == "error":
                        result.error = event.get("error", event.get("message", "Unknown error"))
                        return result

                    elif etype in ("meta", "user_message_id", "memory_used",
                                   "chat_title", "sim_ready", "rate_limited",
                                   "waf_blocked", "account_switched"):
                        pass  # Informational

    result.answer = "".join(answer_parts).strip()
    result.thinking = "".join(thinking_parts).strip()
    return result


# ── Message chunking ─────────────────────────────────────────────────────────
def chunk_message(text: str, max_len: int = _MAX_TG_MESSAGE_LEN) -> list[str]:
    """Split long text into chunks respecting code block boundaries."""
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break

        # Try to split at a newline near the limit
        split_at = remaining.rfind("\n", 0, max_len - 100)
        if split_at < 100:
            split_at = remaining.rfind(" ", 0, max_len - 100)
        if split_at < 100:
            split_at = max_len

        chunk = remaining[:split_at].rstrip()
        remaining = remaining[split_at:].lstrip()

        # Check if we split inside a code block — close and reopen
        open_fences = chunk.count("```")
        if open_fences % 2 == 1:
            last_fence = chunk.rfind("```")
            lang_line = chunk[last_fence:]
            lang = lang_line.split("\n")[0].replace("```", "").strip()
            chunk += "\n```"
            remaining = f"```{lang}\n{remaining}" if lang else f"```\n{remaining}"

        chunks.append(chunk)

    return chunks


# ── Permission resolution ────────────────────────────────────────────────────
async def resolve_permission(tag_id: str, action: str, chat_id: str | None = None) -> dict:
    """Approve or deny a permission request via Sable API.

    Args:
        tag_id: The permission request tag ID.
        action: "approve", "session", or "deny".
        chat_id: Chat session ID (for session-scoped approvals).

    Returns:
        API response dict with ok, feedback fields.
    """
    if action == "deny":
        endpoint = f"/api/skills/deny/{tag_id}"
        body: dict[str, Any] = {"chat_id": chat_id}
    else:
        endpoint = f"/api/skills/approve/{tag_id}"
        body = {"chat_id": chat_id, "session": action == "session"}

    status, resp = await _sable_request("POST", endpoint, json_body=body, timeout=60)
    if status == 200 and isinstance(resp, dict):
        return resp
    return {"ok": False, "error": f"API returned {status}: {str(resp)[:200]}"}


# ── Bot handlers ─────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start — introduce bot and set up session."""
    user = update.effective_user
    logger.info("/start from user=%s name=%s", user.id if user else "unknown", user.first_name if user else "?")
    if not user or not is_user_allowed(user.id):
        logger.warning("/start REJECTED — user %s not in allowed list", user.id if user else "?")
        await update.message.reply_text("⛔ Unauthorized.")
        return

    kb = _build_persistent_keyboard()
    chat_id = get_chat_id_for_user(user.id)
    if chat_id:
        await update.message.reply_text(
            f"👋 Welcome back! You have an active session.\n"
            f"Send me a message to chat with Sable.\n\n"
            f"Use the buttons below or commands:\n"
            f"/new — Start a new chat\n"
            f"/reset — Clear current session\n"
            f"/status — Full session status\n"
            f"/help — Show help",
            reply_markup=kb,
        )
    else:
        await update.message.reply_text(
            f"🤖 Hi {user.first_name}! I'm Sable's Telegram bot.\n"
            f"Send me a message to start chatting.\n\n"
            f"Use the buttons below or commands:\n"
            f"/new — Start a new chat\n"
            f"/status — Full session status\n"
            f"/help — Show help",
            reply_markup=kb,
        )


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /new — create fresh chat session."""
    user = update.effective_user
    logger.info("/new from user=%s", user.id if user else "unknown")
    if not user or not is_user_allowed(user.id):
        logger.warning("/new REJECTED — user %s not allowed", user.id if user else "?")
        return

    kb = _build_persistent_keyboard()
    msg = await update.message.reply_text("⏳ Creating new chat...")
    logger.debug("/new: sent status msg, calling create_new_chat")
    try:
        chat_id = await create_new_chat()
        logger.info("/new: create_new_chat returned %r", chat_id)
    except Exception as e:
        logger.error("create_new_chat raised in cmd_new: %s", e)
        await msg.edit_text(f"❌ Failed to create chat: {type(e).__name__}")
        return
    logger.debug("/new: about to send confirmation")
    if chat_id:
        set_chat_id_for_user(user.id, chat_id)
        await msg.edit_text(
            f"✅ New chat started!\nChat ID: `{chat_id[:8]}...`",
            parse_mode="Markdown",
        )
        logger.info("/new: confirmation sent for chat %s", chat_id[:8])
    else:
        await msg.edit_text("❌ Failed to create chat. Is Sable server running?")
        logger.error("/new: create_new_chat returned None")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /reset — clear session mapping."""
    user = update.effective_user
    logger.info("/reset from user=%s", user.id if user else "unknown")
    if not user or not is_user_allowed(user.id):
        logger.warning("/reset REJECTED — user %s not allowed", user.id if user else "?")
        return
    clear_chat_for_user(user.id)
    await update.message.reply_text("🔄 Session cleared. Send /new or just message me to start fresh.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status — show full session state including persona, model, thinking, context."""
    user = update.effective_user
    logger.info("/status from user=%s", user.id if user else "unknown")
    if not user or not is_user_allowed(user.id):
        logger.warning("/status REJECTED — user %s not allowed", user.id if user else "?")
        return

    msg = await update.message.reply_text("⏳ Gathering status...")
    try:
        # Fetch models count
        status_code, models_body = await _sable_request("GET", "/api/models", timeout=10)
        models_count = 0
        current_model_info = "default"
        if status_code == 200 and isinstance(models_body, dict):
            models_list = models_body.get("models", [])
            models_count = len(models_list)

        # Fetch active persona
        persona_status, persona_body = await _sable_request("GET", "/api/personas", timeout=10)
        active_persona = "None"
        if persona_status == 200 and isinstance(persona_body, dict):
            cfg = persona_body.get("config", {})
            active_persona = cfg.get("active") or "None"

        # User preferences
        prefs = get_all_user_prefs(user.id)
        pref_model = prefs.get("model")
        pref_thinking = prefs.get("thinking_mode")

        # If user has a preferred model, find its label
        if pref_model and status_code == 200 and isinstance(models_body, dict):
            for m in models_body.get("models", []):
                if m.get("id") == pref_model:
                    current_model_info = m.get("label", pref_model)
                    break
            else:
                current_model_info = pref_model

        # Chat session info
        chat_id = get_chat_id_for_user(user.id)
        session_info = f"`{chat_id[:12]}...`" if chat_id else "No active chat"

        # Build status text
        lines = [
            "📊 *Session Status*",
            "",
            f"🎭 *Persona:* {active_persona}",
            f"🤖 *Model:* {current_model_info}",
            f"🧠 *Thinking:* {pref_thinking or 'default'}",
            f"💬 *Chat:* {session_info}",
            f"📡 *Server:* {get_server_url()}",
            f"🔢 *Models available:* {models_count}",
        ]

        # Connection status
        if status_code == 200:
            lines.insert(1, "✅ Connected")
        else:
            lines.insert(1, f"❌ Server error ({status_code})")

        await msg.edit_text("\n".join(lines), parse_mode="Markdown")

    except Exception as e:
        await msg.edit_text(f"❌ Cannot reach Sable server: {e}")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help."""
    user = update.effective_user
    logger.info("/help from user=%s", user.id if user else "unknown")
    kb = _build_persistent_keyboard()
    await update.message.reply_text(
        "🤖 *Sable Telegram Bot*\n\n"
        "Send me a message to chat with Sable AI.\n\n"
        "*Commands:*\n"
        "/start — Welcome & keyboard\n"
        "/new — Start a new chat session\n"
        "/reset — Clear current session\n"
        "/status — Full session status\n"
        "/help — This message\n\n"
        "*Keyboard Buttons:*\n"
        "🤖 Model — Switch AI model\n"
        "🎭 Persona — Change persona\n"
        "🧠 Thinking — Toggle thinking mode\n"
        "🔬 Research — Active research sessions\n"
        "📝 Notes — Recent notes\n"
        "🖼 Gallery — Generated images\n"
        "💬 History — Browse & switch chats\n"
        "📎 Media — Send photos/voice\n\n"
        "*Interactive Features:*\n"
        "• 🔒 Permission requests → Approve/Deny\n"
        "• ❓ Questions → Option buttons\n"
        "• ⚡ Tools → Live status updates",
        parse_mode="Markdown",
        reply_markup=kb,
    )


def _format_tool_activities(activities: list[dict[str, Any]]) -> str:
    """Format tool activities into a readable block for TG messages.

    Each tool shows: name, status icon, duration, and truncated output.
    Mirrors the web UI's tool activity card style.
    """
    if not activities:
        return ""

    lines: list[str] = []
    for act in activities:
        name = act.get("name", "unknown")
        ok = act.get("ok", False)
        duration = act.get("duration_ms", 0)
        output = act.get("output", "")

        icon = "✅" if ok else "❌"
        dur_str = f" ({duration}ms)" if duration else ""
        header = f"{icon} `{name}`{dur_str}"
        lines.append(header)

        if output.strip():
            truncated = _truncate_tool_output(output.strip())
            # Indent output for readability
            indented = "\n".join(f"  {ln}" for ln in truncated.splitlines()[:30])
            lines.append(indented)

    return "\n".join(lines)


def _build_stop_keyboard(chat_id: str) -> InlineKeyboardMarkup:
    """Build inline keyboard with a stop button for active streams."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏹ Stop", callback_data=f"{_STOP_CALLBACK_PREFIX}{chat_id}")]
    ])


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming text messages — forward to Sable and relay response.

    Also handles persistent keyboard button presses to open panels.
    Supports full interactive loop:
    - Normal responses → chunked text messages
    - ask_user → inline keyboard with options
    - permission_request → approve/session/deny buttons
    - Tool execution → live status message updates
    - Keyboard buttons → panel handlers
    """
    user = update.effective_user
    logger.info("MESSAGE from user=%s name=%s", user.id if user else "unknown", user.first_name if user else "?")
    if not user or not is_user_allowed(user.id):
        logger.warning("MESSAGE REJECTED — user %s not allowed", user.id if user else "?")
        return

    text = update.message.text
    logger.debug("MESSAGE TEXT: %r", text)
    if not text or not text.strip():
        logger.debug("MESSAGE EMPTY — skipping")
        return

    text_stripped = text.strip()

    # ── Persistent keyboard button routing ──
    _keyboard_routes = {
        _KB_MODEL: _panel_model,
        _KB_PERSONA: _panel_persona,
        _KB_THINKING: _panel_thinking,
        _KB_RESEARCH: _panel_research,
        _KB_NOTES: _panel_notes,
        _KB_GALLERY: _panel_gallery,
        _KB_HISTORY: _panel_history,
        _KB_MEDIA: _panel_media,
    }
    handler = _keyboard_routes.get(text_stripped)
    if handler:
        await handler(update, context)
        return

    # ── Pending image caption flow ──
    pending_image = context.user_data.pop("pending_image", None)
    if pending_image:
        # User sent text after a photo without caption — combine them
        text = f"[Image: {pending_image}]\n\n{text_stripped}"
        logger.info("Combining pending image %s with caption for user %s", pending_image, user.id)

    # Get or create chat session
    chat_id = get_chat_id_for_user(user.id)
    if not chat_id:
        try:
            chat_id = await create_new_chat()
        except Exception as e:
            logger.error("create_new_chat raised in auto-create: %s", e)
            await update.message.reply_text(f"❌ Could not create chat: {type(e).__name__}")
            return
        if not chat_id:
            await update.message.reply_text("❌ Could not create chat session. Is Sable running?")
            return
        set_chat_id_for_user(user.id, chat_id)
        await update.message.reply_text(
            f"🆕 New chat created: `{chat_id[:8]}...`",
            parse_mode="Markdown",
        )

    # Get user preferences for model/thinking_mode
    user_model = get_user_pref(user.id, "model")
    user_thinking = get_user_pref(user.id, "thinking_mode")

    # Create streaming display with stop button
    stop_kb = _build_stop_keyboard(chat_id)
    stream_display = StreamDisplay(update.message, stop_kb, thinking_enabled=bool(user_thinking))
    await stream_display.start()
    status_msg = stream_display.msg  # For compatibility with post-stream code

    # Contextual status update callback (generic status only — thinking streams separately)
    async def update_status(new_text: str) -> None:
        # Status updates go to a separate ephemeral edit only if no streaming is active
        pass  # Thinking/answer now stream via on_thinking/on_answer

    # Tool event callback — finalizes stream display, shows tool, then resumes
    current_tool_args = ""

    async def send_tool_event(event_type: str, data: dict) -> None:
        nonlocal status_msg, current_tool_args, stream_display
        name = data.get("name", "unknown")
        if event_type == "start":
            # Finalize current stream display before showing tool
            await stream_display.finalize()
            # Extract args from event (dict attrs or string attrs)
            attrs = data.get("data", {}).get("attrs") or data.get("attrs", "")
            if isinstance(attrs, dict):
                args_str = ", ".join(f"{k}={v}" for k, v in attrs.items() if v)
            else:
                args_str = str(attrs).strip() if attrs else ""
            current_tool_args = args_str
            display = f"⚡ `{name}`"
            if args_str:
                display += f"\n`{args_str[:500]}`"
            try:
                status_msg = await update.message.reply_text(display, parse_mode="Markdown", reply_markup=stop_kb)
            except Exception:
                pass
        elif event_type == "end":
            ok = data.get("ok", False)
            duration_ms = data.get("duration_ms", 0)
            output = data.get("output", "")
            icon = "✅" if ok else "❌"
            dur_str = f" ({duration_ms}ms)" if duration_ms else ""
            header = f"{icon} `{name}`{dur_str}"
            if current_tool_args:
                header += f"\n`{current_tool_args[:500]}`"
            msg_text = header
            if output.strip():
                truncated = _truncate_tool_output(output.strip())
                msg_text += f"\n```\n{truncated}\n```"
            # Finalize this tool message (remove stop button — it's a record now)
            try:
                await status_msg.edit_text(msg_text, parse_mode="Markdown", reply_markup=None)
            except Exception:
                pass
            # Send generated images as photos
            skill_result = data.get("result")
            if skill_result:
                await _send_generated_images(update.message, skill_result)
            # Create fresh stream display for resumed streaming
            stream_display = StreamDisplay(update.message, stop_kb, thinking_enabled=bool(user_thinking))
            await stream_display.start()
            status_msg = stream_display.msg
            current_tool_args = ""

    # Track active stream for stop button
    stream_task = asyncio.current_task()
    if stream_task:
        _active_streams[user.id] = stream_task

    # Use closures so callbacks always target the CURRENT stream_display
    # (send_tool_event replaces stream_display after each tool run)
    async def _forward_thinking(text: str) -> None:
        await stream_display.append_thinking(text)

    async def _forward_answer(text: str) -> None:
        await stream_display.append_answer(text)

    # Stream response from Sable with user preferences
    try:
        result = await stream_chat_events(
            chat_id, text,
            model=user_model,
            thinking_mode=user_thinking,
            on_status=update_status,
            on_tool_event=send_tool_event,
            on_thinking=_forward_thinking,
            on_answer=_forward_answer,
        )
    except asyncio.CancelledError:
        # Stop button was pressed
        try:
            await status_msg.edit_text("⏹ Stopped.", reply_markup=None)
        except Exception:
            pass
        return
    except asyncio.TimeoutError:
        await status_msg.edit_text("⏰ Response timed out (5 min). Try again?", reply_markup=None)
        return
    except Exception as e:
        logger.exception("Chat stream failed for user %s", user.id)
        await status_msg.edit_text(f"❌ Error: {e}", reply_markup=None)
        return
    finally:
        _active_streams.pop(user.id, None)

    # Handle error
    if result.error:
        await status_msg.edit_text(f"❌ {result.error}")
        return

    # Handle permission_request → inline keyboard
    if result.permission_request:
        perm = result.permission_request
        tag_id = perm.get("id", "")
        data = perm.get("data", {})
        cmd_preview = data.get("command", "")[:200]
        reason = data.get("reason", "This command requires approval")
        category = data.get("category", "unknown")

        perm_text = (
            f"🔒 *Permission Required*\n\n"
            f"*Category:* {category}\n"
            f"*Command:*\n`{cmd_preview}`\n\n"
            f"_Reason: {reason}_"
        )

        keyboard = [
            [
                InlineKeyboardButton("✓ Approve", callback_data=f"perm_approve:{tag_id}"),
                InlineKeyboardButton("✓ Allow Session", callback_data=f"perm_session:{tag_id}"),
            ],
            [
                InlineKeyboardButton("✗ Deny", callback_data=f"perm_deny:{tag_id}"),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            await status_msg.edit_text(perm_text, parse_mode="Markdown", reply_markup=reply_markup)
        except Exception:
            await status_msg.edit_text(perm_text, reply_markup=reply_markup)

        # Store chat_id in context for callback handler
        context.user_data["pending_perm_chat_id"] = chat_id
        return

    # Handle ask_user → inline keyboard with options
    if result.ask_user_payload:
        payload = result.ask_user_payload
        question = payload.get("question", "Choose an option:")
        options = payload.get("options", [])
        default_idx = payload.get("default")

        # Build keyboard — each option is a button
        keyboard = []
        for i, opt in enumerate(options):
            label = f"▸ {opt}" if i == default_idx else opt
            keyboard.append([InlineKeyboardButton(label, callback_data=f"ask_user:{i}:{opt[:100]}")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        # Include any accumulated answer text before the question
        display_text = ""
        if result.answer:
            display_text = result.answer + "\n\n"
        display_text += f"❓ *{question}*"

        try:
            await status_msg.edit_text(display_text, parse_mode="Markdown", reply_markup=reply_markup)
        except Exception:
            await status_msg.edit_text(display_text, reply_markup=reply_markup)

        return

    # Finalize the stream display (flush any pending tokens, remove stop button)
    await stream_display.finalize()

    # If nothing was streamed, show a helpful fallback message
    if not result.answer and not result.thinking:
        fallback = "⚠️ No response received."
        if result.error:
            fallback = f"❌ {result.error}"
        else:
            fallback += " The model may be locked to a different provider. Try switching models or starting a new chat."
        try:
            await status_msg.edit_text(fallback, reply_markup=None)
        except Exception:
            pass


# ── Panel Handlers (persistent keyboard buttons) ────────────────────────────

async def _panel_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show model selector as inline keyboard."""
    user = update.effective_user
    if not user:
        return
    msg = await update.message.reply_text("⏳ Loading models...")
    try:
        status, body = await _sable_request("GET", "/api/models", timeout=15)
        if status != 200 or not isinstance(body, dict):
            await msg.edit_text(f"❌ Failed to load models ({status})")
            return

        models = body.get("models", [])
        current = get_user_pref(user.id, "model")
        if not models:
            await msg.edit_text("No models available.")
            return

        keyboard = []
        for m in models:
            mid = m["id"]
            label = m.get("label", mid)
            marker = " ✓" if mid == current else ""
            # Truncate callback data to stay within 64 bytes
            cb_data = f"{_CB_SET_MODEL}{mid[:50]}"
            keyboard.append([InlineKeyboardButton(f"{label}{marker}", callback_data=cb_data)])

        await msg.edit_text(
            "🤖 *Select Model*\n_Current: " + (current or "default") + "_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")


async def _panel_persona(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show persona selector as inline keyboard."""
    user = update.effective_user
    if not user:
        return
    msg = await update.message.reply_text("⏳ Loading personas...")
    try:
        status, body = await _sable_request("GET", "/api/personas", timeout=15)
        if status != 200 or not isinstance(body, dict):
            await msg.edit_text(f"❌ Failed to load personas ({status})")
            return

        personas = body.get("personas", [])
        config = body.get("config", {})
        active = config.get("active", "None")

        if not personas:
            await msg.edit_text("No personas available.")
            return

        keyboard = []
        for p in personas:
            name = p["name"]
            marker = " ✓" if p.get("active") else ""
            cb_data = f"{_CB_SET_PERSONA}{name[:50]}"
            keyboard.append([InlineKeyboardButton(f"{name}{marker}", callback_data=cb_data)])

        await msg.edit_text(
            f"🎭 *Select Persona*\n_Active: {active}_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")


async def _panel_thinking(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show thinking mode toggle for current model."""
    user = update.effective_user
    if not user:
        return
    msg = await update.message.reply_text("⏳ Loading thinking modes...")
    try:
        # Get current model's thinking modes
        status, body = await _sable_request("GET", "/api/models", timeout=15)
        if status != 200 or not isinstance(body, dict):
            await msg.edit_text(f"❌ Failed to load models ({status})")
            return

        current_model_id = get_user_pref(user.id, "model")
        current_thinking = get_user_pref(user.id, "thinking_mode")
        models = body.get("models", [])

        # Find thinking modes for current model (or first model with thinking)
        thinking_modes: list[dict] = []
        target_label = "default"
        for m in models:
            if current_model_id and m["id"] == current_model_id:
                thinking_modes = m.get("thinking_modes", [])
                target_label = m.get("label", m["id"])
                break
        else:
            # No specific model set — show first model with thinking modes
            for m in models:
                tm = m.get("thinking_modes", [])
                if tm:
                    thinking_modes = tm
                    target_label = m.get("label", m["id"])
                    break

        if not thinking_modes:
            await msg.edit_text(
                f"🧠 *Thinking Mode*\n_Model: {target_label}_\n\nNo thinking modes available for this model.",
                parse_mode="Markdown",
            )
            return

        keyboard = []
        # Add "off" option
        off_marker = " ✓" if not current_thinking else ""
        keyboard.append([InlineKeyboardButton(f"Off{off_marker}", callback_data=f"{_CB_SET_THINKING}off")])
        for tm in thinking_modes:
            tm_id = tm["id"]
            tm_label = tm.get("label", tm_id)
            marker = " ✓" if tm_id == current_thinking else ""
            keyboard.append([InlineKeyboardButton(f"{tm_label}{marker}", callback_data=f"{_CB_SET_THINKING}{tm_id[:50]}")])

        await msg.edit_text(
            f"🧠 *Thinking Mode*\n_Model: {target_label}_\n_Current: {current_thinking or 'off'}_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")


def _build_library_keyboard(
    items: list[dict],
    cb_prefix: str,
    page: int = 0,
    page_size: int = 8,
    label_key: str = "title",
    sub_key: str | None = None,
) -> tuple[list[list[InlineKeyboardButton]], int]:
    """Build paginated inline keyboard for library items.

    Returns (keyboard_rows, total_pages).
    Each button shows label + optional sub-info, callback sends the file.
    """
    total = len(items)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    end = min(start + page_size, total)
    page_items = items[start:end]

    keyboard: list[list[InlineKeyboardButton]] = []
    for item in page_items:
        fname = item.get("filename", "?")
        label = item.get(label_key, fname)[:40]
        # Truncate filename for callback data (TG 64-byte limit)
        safe_fname = fname[:50]
        cb_data = f"{cb_prefix}{safe_fname}"
        if sub_key:
            sub = item.get(sub_key, "")
            if sub:
                label = f"{label} ({str(sub)[:15]})"
        keyboard.append([InlineKeyboardButton(label, callback_data=cb_data)])

    # Navigation row — derive panel key from cb_prefix for pagination callbacks
    _panel_key_map = {
        _CB_FILE_GALLERY: "gal",
        _CB_FILE_NOTE: "not",
        _CB_FILE_RESEARCH: "res",
    }
    panel_key = _panel_key_map.get(cb_prefix, "")
    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀ Prev", callback_data=f"{_CB_PAGE}{panel_key}:{page - 1}"))
    nav_row.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Next ▶", callback_data=f"{_CB_PAGE}{panel_key}:{page + 1}"))
    if len(nav_row) > 1:
        keyboard.append(nav_row)

    return keyboard, total_pages


async def _panel_research(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show research files as downloadable buttons."""
    msg = await update.message.reply_text("⏳ Loading research...")
    try:
        status, body = await _sable_request("GET", "/api/library/research", timeout=15)
        if status != 200:
            await msg.edit_text(f"❌ Failed to load research ({status})")
            return

        items = body if isinstance(body, list) else []
        if not items:
            await msg.edit_text("🔬 *Research*\n\nNo research files found.", parse_mode="Markdown")
            return

        keyboard, _ = _build_library_keyboard(items, _CB_FILE_RESEARCH, label_key="title")
        await msg.edit_text(
            f"🔬 *Research* ({len(items)} files)\n_Tap a file to download._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")


async def _panel_notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show notes as downloadable buttons."""
    msg = await update.message.reply_text("⏳ Loading notes...")
    try:
        status, body = await _sable_request("GET", "/api/library/notes", timeout=15)
        if status != 200:
            await msg.edit_text(f"❌ Failed to load notes ({status})")
            return

        items = body if isinstance(body, list) else []
        if not items:
            await msg.edit_text("📝 *Notes*\n\nNo notes found.", parse_mode="Markdown")
            return

        keyboard, _ = _build_library_keyboard(items, _CB_FILE_NOTE, label_key="title")
        await msg.edit_text(
            f"📝 *Notes* ({len(items)} files)\n_Tap a note to download._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")


async def _panel_gallery(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show gallery items as downloadable buttons."""
    msg = await update.message.reply_text("⏳ Loading gallery...")
    try:
        status, body = await _sable_request("GET", "/api/library/gallery", timeout=15)
        if status != 200:
            await msg.edit_text(f"❌ Failed to load gallery ({status})")
            return

        items = body if isinstance(body, list) else []
        if not items:
            await msg.edit_text("🖼 *Gallery*\n\nNo images found.", parse_mode="Markdown")
            return

        keyboard, _ = _build_library_keyboard(
            items, _CB_FILE_GALLERY, label_key="filename", sub_key="type",
        )
        await msg.edit_text(
            f"🖼 *Gallery* ({len(items)} images)\n_Tap an image to download._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")


async def _panel_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show chat history with session switching."""
    user = update.effective_user
    if not user:
        return
    msg = await update.message.reply_text("⏳ Loading chat history...")
    try:
        status, body = await _sable_request("GET", "/api/chats", timeout=15)
        if status != 200:
            await msg.edit_text(f"❌ Failed to load chats ({status})")
            return

        chats = body if isinstance(body, list) else body.get("chats", []) if isinstance(body, dict) else []
        current_chat = get_chat_id_for_user(user.id)

        if not chats:
            await msg.edit_text("💬 *Chat History*\n\nNo chats found.", parse_mode="Markdown")
            return

        # Pagination: 10 per page
        page_size = 10
        total = len(chats)
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = 0  # Default first page

        start = page * page_size
        end = min(start + page_size, total)
        page_chats = chats[start:end]

        keyboard = []
        for c in page_chats:
            cid = c.get("id", c.get("chat_id", ""))
            title = c.get("title", c.get("name", "Untitled"))[:35]
            marker = " ✓" if cid == current_chat else ""
            cb_data = f"{_CB_SWITCH_CHAT}{cid[:50]}"
            keyboard.append([InlineKeyboardButton(f"{title}{marker}", callback_data=cb_data)])

        # Pagination row
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("◀ Prev", callback_data=f"{_CB_PAGE}his:{page - 1}"))
        nav_row.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton("Next ▶", callback_data=f"{_CB_PAGE}his:{page + 1}"))
        if nav_row:
            keyboard.append(nav_row)

        await msg.edit_text(
            f"💬 *Chat History* ({total} chats)\n_Current: `{(current_chat or 'none')[:12]}...`_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")


async def _panel_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show media upload info."""
    await update.message.reply_text(
        "📎 *Media*\n\n"
        "Send me a photo or voice message directly.\n"
        "• Photos are saved to uploads\n"
        "• Voice messages are transcribed and sent as chat\n\n"
        "_Just attach media to your message!_",
        parse_mode="Markdown",
    )


# ── Callback query handlers ──────────────────────────────────────────────────
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard button presses for panels, ask_user, and permission_request."""
    query = update.callback_query
    if not query or not query.data:
        return

    user = update.effective_user
    logger.info("CALLBACK from user=%s data=%r", user.id if user else "unknown", query.data)
    if not user or not is_user_allowed(user.id):
        logger.warning("CALLBACK REJECTED — user %s not allowed", user.id if user else "?")
        await query.answer("⛔ Unauthorized", show_alert=True)
        return

    data = query.data

    # ── Stop button pressed ──
    if data.startswith(_STOP_CALLBACK_PREFIX):
        chat_id = data[len(_STOP_CALLBACK_PREFIX):]
        await query.answer("⏹ Stopping...")

        # Call stop API
        try:
            status, resp = await _sable_request(
                "POST", "/api/chat/stop",
                json_body={"chat_id": chat_id},
                timeout=10,
            )
            stopped = resp.get("success", False) if isinstance(resp, dict) else False
        except Exception as e:
            logger.warning("Stop request failed: %s", e)
            stopped = False

        # Cancel the active stream task if tracked
        task = _active_streams.pop(user.id, None)
        if task and not task.done():
            task.cancel()

        # Remove stop button from message
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        if stopped:
            await query.message.reply_text("⏹ Generation stopped.")
        else:
            await query.message.reply_text("⏹ Stop requested (stream may have already finished).")
        return

    # ── Panel selection callbacks ──

    # Set model
    if data.startswith(_CB_SET_MODEL):
        model_id = data[len(_CB_SET_MODEL):]
        set_user_pref(user.id, "model", model_id)
        await query.answer(f"✅ Model set: {model_id[:30]}")
        try:
            await query.edit_message_text(
                f"🤖 *Model Changed*\n\nSelected: `{model_id}`\n_This will apply to your next message._",
                parse_mode="Markdown",
                reply_markup=None,
            )
        except Exception:
            pass
        return

    # Set persona
    if data.startswith(_CB_SET_PERSONA):
        persona_name = data[len(_CB_SET_PERSONA):]
        # Call API to set active persona server-side
        api_status, api_resp = await _sable_request(
            "PUT", "/api/personas/active",
            json_body={"name": persona_name},
            timeout=15,
        )
        if api_status == 200:
            set_user_pref(user.id, "persona", persona_name)
            await query.answer(f"✅ Persona: {persona_name}")
            try:
                await query.edit_message_text(
                    f"🎭 *Persona Changed*\n\nActive: *{persona_name}*",
                    parse_mode="Markdown",
                    reply_markup=None,
                )
            except Exception:
                pass
        else:
            await query.answer(f"❌ Failed: {str(api_resp)[:50]}", show_alert=True)
        return

    # Set thinking mode
    if data.startswith(_CB_SET_THINKING):
        mode_id = data[len(_CB_SET_THINKING):]
        if mode_id == "off":
            set_user_pref(user.id, "thinking_mode", None)
            await query.answer("✅ Thinking: off")
            display = "Off"
        else:
            set_user_pref(user.id, "thinking_mode", mode_id)
            await query.answer(f"✅ Thinking: {mode_id[:30]}")
            display = mode_id
        try:
            await query.edit_message_text(
                f"🧠 *Thinking Mode Changed*\n\nMode: *{display}*\n_Applies to next message._",
                parse_mode="Markdown",
                reply_markup=None,
            )
        except Exception:
            pass
        return

    # Switch chat session
    if data.startswith(_CB_SWITCH_CHAT):
        new_chat_id = data[len(_CB_SWITCH_CHAT):]
        set_chat_id_for_user(user.id, new_chat_id)
        await query.answer(f"✅ Switched to {new_chat_id[:12]}...")
        try:
            await query.edit_message_text(
                f"💬 *Chat Switched*\n\nActive: `{new_chat_id[:16]}...`",
                parse_mode="Markdown",
                reply_markup=None,
            )
        except Exception:
            pass
        return

    # ── ask_user option selected ──
    if data.startswith("ask_user:"):
        parts = data.split(":", 2)
        if len(parts) < 3:
            await query.answer("Invalid option", show_alert=True)
            return

        option_text = parts[2]
        await query.answer(f"Selected: {option_text[:50]}")

        # Edit message to show selection
        try:
            await query.edit_message_text(
                f"{query.message.text}\n\n→ *{option_text}*",
                parse_mode="Markdown",
            )
        except Exception:
            pass

        # Send the selected option as a new user message to continue the agent loop
        chat_id = get_chat_id_for_user(user.id)
        if chat_id:
            stop_kb = _build_stop_keyboard(chat_id)
            _cb_thinking = get_user_pref(user.id, "thinking_mode")

            # Streaming display for thinking + answer
            stream_display = StreamDisplay(query.message, stop_kb, thinking_enabled=bool(_cb_thinking))
            await stream_display.start()
            status_msg = stream_display.msg

            async def update_status(new_text: str) -> None:
                pass  # Thinking/answer now stream via on_thinking/on_answer

            # Tool event callback — finalizes stream, shows tool, recreates stream
            current_tool_args = ""

            async def send_tool_event(event_type: str, data: dict) -> None:
                nonlocal status_msg, current_tool_args, stream_display
                name = data.get("name", "unknown")
                if event_type == "start":
                    await stream_display.finalize()
                    attrs = data.get("data", {}).get("attrs") or data.get("attrs", "")
                    if isinstance(attrs, dict):
                        args_str = ", ".join(f"{k}={v}" for k, v in attrs.items() if v)
                    else:
                        args_str = str(attrs).strip() if attrs else ""
                    current_tool_args = args_str
                    display = f"⚡ `{name}`"
                    if args_str:
                        display += f"\n`{args_str[:500]}`"
                    try:
                        status_msg = await query.message.reply_text(display, parse_mode="Markdown", reply_markup=stop_kb)
                    except Exception:
                        pass
                elif event_type == "end":
                    ok = data.get("ok", False)
                    duration_ms = data.get("duration_ms", 0)
                    output = data.get("output", "")
                    icon = "✅" if ok else "❌"
                    dur_str = f" ({duration_ms}ms)" if duration_ms else ""
                    header = f"{icon} `{name}`{dur_str}"
                    if current_tool_args:
                        header += f"\n`{current_tool_args[:500]}`"
                    msg_text = header
                    if output.strip():
                        truncated = _truncate_tool_output(output.strip())
                        msg_text += f"\n```\n{truncated}\n```"
                    try:
                        await status_msg.edit_text(msg_text, parse_mode="Markdown", reply_markup=None)
                    except Exception:
                        pass
                    # Send generated images as photos
                    skill_result = data.get("result")
                    if skill_result:
                        await _send_generated_images(query.message, skill_result)
                    # Create fresh stream display for resumed streaming
                    stream_display = StreamDisplay(query.message, stop_kb, thinking_enabled=bool(_cb_thinking))
                    await stream_display.start()
                    status_msg = stream_display.msg
                    current_tool_args = ""

            # Track stream for stop button
            stream_task = asyncio.current_task()
            if stream_task:
                _active_streams[user.id] = stream_task

            try:
                u_model = get_user_pref(user.id, "model")
                u_thinking = get_user_pref(user.id, "thinking_mode")
                result = await stream_chat_events(
                    chat_id, option_text,
                    model=u_model,
                    thinking_mode=u_thinking,
                    on_status=update_status,
                    on_tool_event=send_tool_event,
                    on_thinking=lambda t: stream_display.append_thinking(t),
                    on_answer=lambda a: stream_display.append_answer(a),
                )
            except asyncio.CancelledError:
                try:
                    await status_msg.edit_text("⏹ Stopped.", reply_markup=None)
                except Exception:
                    pass
                return
            except Exception as e:
                await status_msg.edit_text(f"❌ Error: {e}", reply_markup=None)
                return
            finally:
                _active_streams.pop(user.id, None)

            # Handle interactive results (permissions, ask_user)
            if result.error:
                await status_msg.edit_text(f"❌ {result.error}")
                return
            if result.permission_request or result.ask_user_payload:
                await stream_display.finalize()
                await _send_stream_result(query.message, result, context, chat_id)
                return

            # Finalize the stream display
            await stream_display.finalize()
            if not result.answer and not result.thinking:
                err_msg = f"❌ {result.error}" if result.error else "⚠️ No response received. The model may be locked or unavailable. Try switching models."
                try:
                    await status_msg.edit_text(err_msg, reply_markup=None)
                except Exception:
                    pass

        return

    # ── Permission approve/session/deny ──
    if data.startswith("perm_"):
        parts = data.split(":", 1)
        if len(parts) < 2:
            await query.answer("Invalid action", show_alert=True)
            return

        action_type = parts[0].replace("perm_", "")  # approve, session, deny
        tag_id = parts[1]
        chat_id = context.user_data.get("pending_perm_chat_id")

        # Disable buttons
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        if action_type == "deny":
            await query.edit_message_text(
                f"{query.message.text}\n\n❌ *Denied*",
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text(
                f"{query.message.text}\n\n✅ *{'Approved for session' if action_type == 'session' else 'Approved'}* — executing...",
                parse_mode="Markdown",
            )

        # Resolve permission via API
        api_result = await resolve_permission(tag_id, action_type, chat_id)

        if not api_result.get("ok"):
            await query.message.reply_text(f"❌ Permission error: {api_result.get('error', 'Unknown')}")
            return

        feedback = api_result.get("feedback", "")
        if feedback:
            # Send tool feedback as a status update
            await query.message.reply_text(f"⚡ {feedback[:500]}")

        # Auto-continue: send empty message to trigger next agent turn
        # The Sable backend auto-continues after approval, so we need to poll
        # Actually — the approve endpoint returns feedback but doesn't auto-continue.
        # We need to send a follow-up message to continue the conversation.
        if chat_id:
            stop_kb = _build_stop_keyboard(chat_id)
            _perm_thinking = get_user_pref(user.id, "thinking_mode")

            # Streaming display for thinking + answer
            stream_display = StreamDisplay(query.message, stop_kb, thinking_enabled=bool(_perm_thinking))
            await stream_display.start()
            status_msg = stream_display.msg

            async def update_status(new_text: str) -> None:
                pass  # Thinking/answer now stream via on_thinking/on_answer

            # Tool event callback — finalizes stream, shows tool, recreates stream
            current_tool_args = ""

            async def send_tool_event(event_type: str, data: dict) -> None:
                nonlocal status_msg, current_tool_args, stream_display
                name = data.get("name", "unknown")
                if event_type == "start":
                    await stream_display.finalize()
                    attrs = data.get("data", {}).get("attrs") or data.get("attrs", "")
                    if isinstance(attrs, dict):
                        args_str = ", ".join(f"{k}={v}" for k, v in attrs.items() if v)
                    else:
                        args_str = str(attrs).strip() if attrs else ""
                    current_tool_args = args_str
                    display = f"⚡ `{name}`"
                    if args_str:
                        display += f"\n`{args_str[:500]}`"
                    try:
                        status_msg = await query.message.reply_text(display, parse_mode="Markdown", reply_markup=stop_kb)
                    except Exception:
                        pass
                elif event_type == "end":
                    ok = data.get("ok", False)
                    duration_ms = data.get("duration_ms", 0)
                    output = data.get("output", "")
                    icon = "✅" if ok else "❌"
                    dur_str = f" ({duration_ms}ms)" if duration_ms else ""
                    header = f"{icon} `{name}`{dur_str}"
                    if current_tool_args:
                        header += f"\n`{current_tool_args[:500]}`"
                    msg_text = header
                    if output.strip():
                        truncated = _truncate_tool_output(output.strip())
                        msg_text += f"\n```\n{truncated}\n```"
                    try:
                        await status_msg.edit_text(msg_text, parse_mode="Markdown", reply_markup=None)
                    except Exception:
                        pass
                    # Send generated images as photos
                    skill_result = data.get("result")
                    if skill_result:
                        await _send_generated_images(query.message, skill_result)
                    # Create fresh stream display for resumed streaming
                    stream_display = StreamDisplay(query.message, stop_kb, thinking_enabled=bool(_perm_thinking))
                    await stream_display.start()
                    status_msg = stream_display.msg
                    current_tool_args = ""

            # Track stream for stop button
            stream_task = asyncio.current_task()
            if stream_task:
                _active_streams[user.id] = stream_task

            try:
                u_model = get_user_pref(user.id, "model")
                u_thinking = get_user_pref(user.id, "thinking_mode")
                result = await stream_chat_events(
                    chat_id, "continue",
                    model=u_model,
                    thinking_mode=u_thinking,
                    on_status=update_status,
                    on_tool_event=send_tool_event,
                    on_thinking=lambda t: stream_display.append_thinking(t),
                    on_answer=lambda a: stream_display.append_answer(a),
                )
            except asyncio.CancelledError:
                try:
                    await status_msg.edit_text("⏹ Stopped.", reply_markup=None)
                except Exception:
                    pass
                return
            except Exception as e:
                await status_msg.edit_text(f"❌ Error continuing: {e}", reply_markup=None)
                return
            finally:
                _active_streams.pop(user.id, None)

            # Handle interactive results
            if result.error:
                await status_msg.edit_text(f"❌ {result.error}")
                return
            if result.permission_request or result.ask_user_payload:
                await stream_display.finalize()
                await _send_stream_result(query.message, result, context, chat_id)
                return

            # Finalize the stream display
            await stream_display.finalize()
            if not result.answer and not result.thinking:
                err_msg = f"❌ {result.error}" if result.error else "⚠️ No response received. The model may be locked or unavailable. Try switching models."
                try:
                    await status_msg.edit_text(err_msg, reply_markup=None)
                except Exception:
                    pass

        return

    # ── Pagination callbacks ──
    if data.startswith(_CB_PAGE):
        parts = data.split(":", 1)
        if len(parts) < 2:
            await query.answer("Invalid page", show_alert=True)
            return
        panel_page = parts[1]  # e.g. "his:2"
        # Re-render the appropriate panel with new page
        # For now, only history pagination is supported
        if panel_page.startswith("his:"):
            try:
                page_num = int(panel_page.split(":")[1])
            except (ValueError, IndexError):
                await query.answer("Invalid page number", show_alert=True)
                return

            # Fetch chats and re-render with new page
            status, body = await _sable_request("GET", "/api/chats", timeout=15)
            if status != 200:
                await query.answer(f"Failed to load chats ({status})", show_alert=True)
                return

            chats = body if isinstance(body, list) else body.get("chats", []) if isinstance(body, dict) else []
            current_chat = get_chat_id_for_user(user.id)

            page_size = 10
            total = len(chats)
            total_pages = max(1, (total + page_size - 1) // page_size)
            page_num = max(0, min(page_num, total_pages - 1))

            start = page_num * page_size
            end = min(start + page_size, total)
            page_chats = chats[start:end]

            keyboard = []
            for c in page_chats:
                cid = c.get("id", c.get("chat_id", ""))
                title = c.get("title", c.get("name", "Untitled"))[:35]
                marker = " ✓" if cid == current_chat else ""
                cb_data = f"{_CB_SWITCH_CHAT}{cid[:50]}"
                keyboard.append([InlineKeyboardButton(f"{title}{marker}", callback_data=cb_data)])

            nav_row = []
            if page_num > 0:
                nav_row.append(InlineKeyboardButton("◀ Prev", callback_data=f"{_CB_PAGE}his:{page_num - 1}"))
            nav_row.append(InlineKeyboardButton(f"{page_num + 1}/{total_pages}", callback_data="noop"))
            if page_num < total_pages - 1:
                nav_row.append(InlineKeyboardButton("Next ▶", callback_data=f"{_CB_PAGE}his:{page_num + 1}"))
            if nav_row:
                keyboard.append(nav_row)

            try:
                await query.edit_message_text(
                    f"💬 *Chat History* ({total} chats)\n_Current: `{(current_chat or 'none')[:12]}...`_",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            except Exception:
                pass
            await query.answer()
            return

        # Fall through to library panel pagination below

        # ── Library panel pagination (gallery, notes, research) ──
        _lib_panel_map = {
            "gal": {"endpoint": "/api/library/gallery", "cb_prefix": _CB_FILE_GALLERY, "label_key": "filename", "sub_key": "type", "title": "🖼 *Gallery*", "hint": "Tap an image to download."},
            "not": {"endpoint": "/api/library/notes", "cb_prefix": _CB_FILE_NOTE, "label_key": "title", "sub_key": None, "title": "📝 *Notes*", "hint": "Tap a note to download."},
            "res": {"endpoint": "/api/library/research", "cb_prefix": _CB_FILE_RESEARCH, "label_key": "title", "sub_key": None, "title": "🔬 *Research*", "hint": "Tap a file to download."},
        }
        for pkey, pinfo in _lib_panel_map.items():
            if panel_page.startswith(f"{pkey}:"):
                try:
                    page_num = int(panel_page.split(":")[1])
                except (ValueError, IndexError):
                    await query.answer("Invalid page number", show_alert=True)
                    return
                status, body = await _sable_request("GET", pinfo["endpoint"], timeout=15)
                if status != 200:
                    await query.answer(f"Failed to load ({status})", show_alert=True)
                    return
                items = body if isinstance(body, list) else []
                keyboard, total_pages = _build_library_keyboard(
                    items, pinfo["cb_prefix"], page=page_num,
                    label_key=pinfo["label_key"], sub_key=pinfo.get("sub_key"),
                )
                try:
                    await query.edit_message_text(
                        f"{pinfo['title']} ({len(items)} items)\n_{pinfo['hint']}_",
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(keyboard),
                    )
                except Exception as e:
                    logger.warning("Library pagination edit failed: %s", e)
                await query.answer()
                return

        await query.answer("Unknown panel", show_alert=True)
        return

    # ── File download callbacks ──

    if data.startswith(_CB_FILE_GALLERY):
        filename = data[len(_CB_FILE_GALLERY):]
        await query.answer("⏳ Sending image...")
        try:
            # Gallery items have a url field like /system/uploads/fname or /assets/fname
            # First try to get the item metadata to find the correct URL
            status, body = await _sable_request("GET", "/api/library/gallery", timeout=15)
            url_path = None
            if status == 200 and isinstance(body, list):
                for item in body:
                    if item.get("filename") == filename:
                        url_path = item.get("url")
                        break
            if not url_path:
                url_path = f"/system/uploads/{filename}"

            dl_status, img_data = await _sable_download(url_path, timeout=30)
            if dl_status != 200 or not img_data:
                await query.message.reply_text(f"❌ Failed to download `{filename}` ({dl_status})")
                return

            import io
            ext = Path(filename).suffix.lower()
            photo_exts = {".png", ".jpg", ".jpeg", ".webp"}
            if ext in photo_exts:
                await query.message.reply_photo(
                    photo=io.BytesIO(img_data),
                    caption=f"🖼 `{filename}`",
                    parse_mode="Markdown",
                )
            else:
                await query.message.reply_document(
                    document=io.BytesIO(img_data),
                    filename=filename,
                    caption=f"🖼 `{filename}`",
                    parse_mode="Markdown",
                )
        except Exception as e:
            await query.message.reply_text(f"❌ Error sending image: {e}")
        return

    if data.startswith(_CB_FILE_NOTE):
        filename = data[len(_CB_FILE_NOTE):]
        await query.answer("⏳ Sending note...")
        try:
            status, body = await _sable_request(
                "GET", f"/api/library/read/notes/{filename}", timeout=15,
            )
            if status != 200 or not isinstance(body, dict) or "content" not in body:
                await query.message.reply_text(f"❌ Failed to read note `{filename}` ({status})")
                return

            content = body["content"]
            import io
            await query.message.reply_document(
                document=io.BytesIO(content.encode("utf-8")),
                filename=filename,
                caption=f"📝 `{filename}`",
                parse_mode="Markdown",
            )
        except Exception as e:
            await query.message.reply_text(f"❌ Error sending note: {e}")
        return

    if data.startswith(_CB_FILE_RESEARCH):
        filename = data[len(_CB_FILE_RESEARCH):]
        await query.answer("⏳ Sending research...")
        try:
            status, body = await _sable_request(
                "GET", f"/api/library/read/research/{filename}", timeout=15,
            )
            if status != 200 or not isinstance(body, dict) or "content" not in body:
                await query.message.reply_text(f"❌ Failed to read research `{filename}` ({status})")
                return

            content = body["content"]
            import io
            await query.message.reply_document(
                document=io.BytesIO(content.encode("utf-8")),
                filename=filename,
                caption=f"🔬 `{filename}`",
                parse_mode="Markdown",
            )
        except Exception as e:
            await query.message.reply_text(f"❌ Error sending research: {e}")
        return

    # Noop callback (page indicator button — does nothing)
    if data == "noop":
        await query.answer()
        return

    await query.answer()


async def _send_stream_result(
    message: Any,
    result: StreamResult,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: str,
) -> None:
    """Send a StreamResult back to the user — handles all interactive events.

    Shared between handle_message and callback query handlers.
    """
    # Handle error
    if result.error:
        await message.reply_text(f"❌ {result.error}")
        return

    # Handle permission_request
    if result.permission_request:
        perm = result.permission_request
        tag_id = perm.get("id", "")
        data = perm.get("data", {})
        cmd_preview = data.get("command", "")[:200]
        reason = data.get("reason", "Requires approval")
        category = data.get("category", "unknown")

        perm_text = (
            f"🔒 *Permission Required*\n\n"
            f"*Category:* {category}\n"
            f"*Command:*\n`{cmd_preview}`\n\n"
            f"_Reason: {reason}_"
        )

        keyboard = [
            [
                InlineKeyboardButton("✓ Approve", callback_data=f"perm_approve:{tag_id}"),
                InlineKeyboardButton("✓ Allow Session", callback_data=f"perm_session:{tag_id}"),
            ],
            [InlineKeyboardButton("✗ Deny", callback_data=f"perm_deny:{tag_id}")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            await message.reply_text(perm_text, parse_mode="Markdown", reply_markup=reply_markup)
        except Exception:
            await message.reply_text(perm_text, reply_markup=reply_markup)

        context.user_data["pending_perm_chat_id"] = chat_id
        return

    # Handle ask_user
    if result.ask_user_payload:
        payload = result.ask_user_payload
        question = payload.get("question", "Choose:")
        options = payload.get("options", [])
        default_idx = payload.get("default")

        keyboard = []
        for i, opt in enumerate(options):
            label = f"▸ {opt}" if i == default_idx else opt
            keyboard.append([InlineKeyboardButton(label, callback_data=f"ask_user:{i}:{opt[:100]}")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        display_text = ""
        if result.answer:
            display_text = result.answer + "\n\n"
        display_text += f"❓ *{question}*"

        try:
            await message.reply_text(display_text, parse_mode="Markdown", reply_markup=reply_markup)
        except Exception:
            await message.reply_text(display_text, reply_markup=reply_markup)
        return

    # Normal response
    response = result.answer
    if not response:
        response = f"❌ {result.error}" if result.error else "⚠️ No response received. Try switching models or starting a new chat."

    if result.thinking and len(result.thinking) < 500:
        response = f"💭 _{result.thinking[:300]}_\n\n{response}"

    # Tool activities are now sent as individual messages during streaming

    chunks = chunk_message(response)
    for chunk_text in chunks:
        try:
            await message.reply_text(chunk_text, parse_mode="Markdown")
        except Exception:
            try:
                await message.reply_text(chunk_text)
            except Exception:
                pass


# ── Handler registration helper ──────────────────────────────────────────────
async def _check_model_image_support(user_id: int) -> tuple[bool, str]:
    """Check if the user's current model supports image input.

    Returns (supported, model_label).
    """
    user_model = get_user_pref(user_id, "model")
    try:
        status, body = await _sable_request("GET", "/api/models", timeout=10)
        if status == 200 and isinstance(body, dict):
            models = body.get("models", [])
            for m in models:
                mid = m.get("id", "")
                # Match by exact ID or if no model set (default)
                if mid == user_model or (not user_model and m.get("_default")):
                    caps = m.get("capabilities", {})
                    label = m.get("label", mid)
                    return caps.get("image", False), label
            # Model not found in list — assume no image support if explicitly set
            if user_model:
                return False, user_model
    except Exception as e:
        logger.warning("Failed to check model capabilities: %s", e)
    # Default: assume supported if we can't verify
    return True, user_model or "default"


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming photos — check model support, save, and send to model."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return

    # Check if current model supports image input
    supported, model_label = await _check_model_image_support(user.id)
    if not supported:
        await update.message.reply_text(
            f"⚠️ *{model_label}* doesn't support image input.\n"
            f"Switch to a vision-capable model to send photos.",
            parse_mode="Markdown",
        )
        return

    photo = update.message.photo[-1]  # Highest resolution
    file = await context.bot.get_file(photo.file_id)

    # Save to uploads directory
    uploads_dir = _SYSTEM_DIR / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    filename = f"tg_{user.id}_{photo.file_unique_id}.jpg"
    filepath = uploads_dir / filename

    await file.download_to_drive(str(filepath))
    size_kb = photo.file_size // 1024 if photo.file_size else "?"

    caption = update.message.caption
    if caption:
        # Photo has caption — send to model with image reference
        text = f"[Image: {filepath}]\n\n{caption}"
        update.message.text = text
        await handle_message(update, context)
    else:
        # No caption — store pending image and ask for caption
        context.user_data["pending_image"] = str(filepath)
        await update.message.reply_text(
            f"📸 Photo received! (`{size_kb}KB`, {photo.width}×{photo.height})\n\n"
            f"Send a caption/message to describe what you'd like me to do with this image.",
            parse_mode="Markdown",
        )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle voice/audio messages — transcribe via STT endpoint, then send to chat."""
    import aiohttp
    import tempfile
    import os

    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return

    voice = update.message.voice or update.message.audio
    if not voice:
        return

    duration = voice.duration if hasattr(voice, "duration") else 0
    mime = voice.mime_type if hasattr(voice, "mime_type") else "unknown"

    # Download audio from Telegram
    status_msg = await update.message.reply_text("🎤 Transcribing voice message...")
    try:
        tg_file = await context.bot.get_file(voice.file_id)
        suffix = ".ogg"  # Telegram voice messages are OGG/Opus by default
        if mime and "/" in mime:
            ext_map = {
                "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/wav": ".wav",
                "audio/webm": ".webm", "audio/ogg": ".ogg", "audio/x-m4a": ".m4a",
            }
            suffix = ext_map.get(mime, ".ogg")

        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = tmp.name
        await tg_file.download_to_drive(tmp_path)
    except Exception as e:
        logger.error("Voice download failed: %s", e)
        await status_msg.edit_text(f"❌ Failed to download audio: {e}")
        return

    # Send to STT transcription endpoint
    try:
        url = f"{get_server_url()}/api/stt/transcribe"
        headers = {"Authorization": f"Bearer {get_auth_token()}"}

        async with aiohttp.ClientSession() as session:
            data = aiohttp.FormData()
            data.add_field(
                "file", open(tmp_path, "rb"),
                filename=f"voice{suffix}",
                content_type=mime or "audio/ogg",
            )
            async with session.post(
                url, data=data, headers=headers,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    logger.error("STT transcription failed (%d): %s", resp.status, err[:200])
                    await status_msg.edit_text(f"❌ Transcription failed: {err[:300]}")
                    return
                stt_result = await resp.json()
    except Exception as e:
        logger.error("STT request failed: %s", e)
        await status_msg.edit_text(f"❌ Transcription error: {e}")
        return
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    transcribed_text = stt_result.get("text", "").strip()
    if not transcribed_text:
        filtered = stt_result.get("filtered", "")
        if filtered == "hallucination":
            await status_msg.edit_text("🎤 Could not transcribe — audio was unclear or silent.")
        elif filtered == "tts_echo":
            await status_msg.edit_text("🎤 Skipped — detected TTS echo feedback loop.")
        else:
            await status_msg.edit_text("🎤 Transcription returned empty text.")
        return

    # Show transcribed text briefly
    preview = transcribed_text[:200] + ("..." if len(transcribed_text) > 200 else "")
    await status_msg.edit_text(f"🎤 _Transcribed:_ \"{preview}\"", parse_mode="Markdown")

    # Now send transcribed text to chat API — same flow as handle_message
    chat_id = get_chat_id_for_user(user.id)
    if not chat_id:
        try:
            chat_id = await create_new_chat()
        except Exception as e:
            logger.error("create_new_chat raised in voice handler: %s", e)
            await update.message.reply_text(f"❌ Could not create chat: {type(e).__name__}")
            return
        if not chat_id:
            await update.message.reply_text("❌ Could not create chat session. Is Sable running?")
            return
        set_chat_id_for_user(user.id, chat_id)

    user_model = get_user_pref(user.id, "model")
    user_thinking = get_user_pref(user.id, "thinking_mode")

    stop_kb = _build_stop_keyboard(chat_id)

    # Streaming display for thinking + answer
    stream_display = StreamDisplay(update.message, stop_kb, thinking_enabled=bool(user_thinking))
    await stream_display.start()
    chat_status_msg = stream_display.msg

    async def update_status(new_text: str) -> None:
        pass  # Thinking/answer now stream via on_thinking/on_answer

    current_tool_args = ""
    async def send_tool_event(event_type: str, data: dict) -> None:
        nonlocal chat_status_msg, current_tool_args, stream_display
        name = data.get("name", "unknown")
        if event_type == "start":
            await stream_display.finalize()
            attrs = data.get("data", {}).get("attrs") or data.get("attrs", "")
            if isinstance(attrs, dict):
                args_str = ", ".join(f"{k}={v}" for k, v in attrs.items() if v)
            else:
                args_str = str(attrs).strip() if attrs else ""
            current_tool_args = args_str
            display = f"⚡ `{name}`"
            if args_str:
                display += f"\n`{args_str[:500]}`"
            try:
                chat_status_msg = await update.message.reply_text(display, parse_mode="Markdown", reply_markup=stop_kb)
            except Exception:
                pass
        elif event_type == "end":
            ok = data.get("ok", False)
            duration_ms = data.get("duration_ms", 0)
            output = data.get("output", "")
            icon = "✅" if ok else "❌"
            dur_str = f" ({duration_ms}ms)" if duration_ms else ""
            header = f"{icon} `{name}`{dur_str}"
            if current_tool_args:
                header += f"\n`{current_tool_args[:500]}`"
            msg_text = header
            if output.strip():
                truncated = _truncate_tool_output(output.strip())
                msg_text += f"\n```\n{truncated}\n```"
            try:
                await chat_status_msg.edit_text(msg_text, parse_mode="Markdown", reply_markup=None)
            except Exception:
                pass
            # Send generated images as photos
            skill_result = data.get("result")
            if skill_result:
                await _send_generated_images(update.message, skill_result)
            # Create fresh stream display for resumed streaming
            stream_display = StreamDisplay(update.message, stop_kb, thinking_enabled=bool(user_thinking))
            await stream_display.start()
            chat_status_msg = stream_display.msg
            current_tool_args = ""

    stream_task = asyncio.current_task()
    if stream_task:
        _active_streams[user.id] = stream_task

    try:
        result = await stream_chat_events(
            chat_id, transcribed_text,
            model=user_model,
            thinking_mode=user_thinking,
            on_status=update_status,
            on_tool_event=send_tool_event,
            on_thinking=lambda t: stream_display.append_thinking(t),
            on_answer=lambda a: stream_display.append_answer(a),
        )
    except asyncio.CancelledError:
        try:
            await chat_status_msg.edit_text("⏹ Stopped.", reply_markup=None)
        except Exception:
            pass
        return
    except asyncio.TimeoutError:
        await chat_status_msg.edit_text("⏰ Response timed out (5 min). Try again?", reply_markup=None)
        return
    except Exception as e:
        logger.exception("Chat stream failed for voice message (user %s)", user.id)
        await chat_status_msg.edit_text(f"❌ Error: {e}", reply_markup=None)
        return
    finally:
        _active_streams.pop(user.id, None)

    # Handle interactive results
    if result.error:
        await chat_status_msg.edit_text(f"❌ {result.error}")
        return
    if result.permission_request or result.ask_user_payload:
        await stream_display.finalize()
        await _send_stream_result(update.message, result, context, chat_id)
        return

    # Finalize the stream display
    await stream_display.finalize()
    if not result.answer and not result.thinking:
        err_msg = f"❌ {result.error}" if result.error else "⚠️ No response received. The model may be locked or unavailable. Try switching models."
        try:
            await chat_status_msg.edit_text(err_msg, reply_markup=None)
        except Exception:
            pass


def register_handlers(app: Application) -> None:
    """Register all bot handlers on an Application instance."""
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))


BOT_COMMANDS = [
    BotCommand("new", "Start a new chat"),
    BotCommand("reset", "Clear current session"),
    BotCommand("status", "Check connection"),
    BotCommand("help", "Show help"),
]


async def _post_init(application: Application) -> None:
    """Set bot commands menu on startup."""
    try:
        await application.bot.set_my_commands(BOT_COMMANDS)
        logger.info("Bot commands registered")
    except Exception as e:
        logger.warning("Failed to set bot commands: %s", e)


# ── Main entry point ─────────────────────────────────────────────────────────
def main() -> None:
    cfg = load_config()
    bot_token = cfg.get("bot_token", "")

    if not bot_token:
        print("❌ No bot token configured.")
        print(f"   Create {_CONFIG_PATH} with:")
        print('   {"bot_token": "YOUR_BOT_TOKEN", "server_url": "http://localhost:61770"}')
        sys.exit(1)

    logging.basicConfig(
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        level=logging.INFO,
    )

    app = Application.builder().token(bot_token).build()
    register_handlers(app)
    app.post_init = _post_init

    logger.info("Starting Sable Telegram Bot (polling mode)...")
    logger.info("Server: %s", get_server_url())
    logger.info("Allowed users: %s", get_allowed_users() or "ALL")

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


# ── Background runner for Sable server integration ───────────────────────────
_bot_app: Application | None = None


async def start_bot_in_background() -> None:
    """Start the Telegram bot as an asyncio task inside Sable's event loop.

    Called from server/api/application.py lifespan when a bot token is configured.
    Runs polling in the background without blocking the server.
    """
    global _bot_app

    cfg = load_config()
    bot_token = cfg.get("bot_token", "")
    if not bot_token:
        logger.warning("Telegram Bot: no token configured, skipping auto-start")
        return

    logging.basicConfig(
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        level=logging.INFO,
    )

    logger.info("Auto-start: building application with token=%s...%s", bot_token[:6], bot_token[-4:])
    _bot_app = Application.builder().token(bot_token).build()
    register_handlers(_bot_app)
    _bot_app.post_init = _post_init

    logger.info("Auto-start: server=%s, allowed=%s", get_server_url(), get_allowed_users() or "ALL")
    logger.info("Starting Sable Telegram Bot (background polling mode)...")
    logger.info("Server: %s", get_server_url())
    logger.info("Allowed users: %s", get_allowed_users() or "ALL")

    # Wait for Sable server to be ready before polling
    server_url = get_server_url()
    logger.info("Auto-start: waiting for server at %s...", server_url)
    import aiohttp
    for attempt in range(30):
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(f"{server_url}/api/health", timeout=aiohttp.ClientTimeout(total=2)) as resp:
                    if resp.status < 500:
                        logger.info("Auto-start: server ready (attempt %d)", attempt + 1)
                        break
        except Exception:
            pass
        await asyncio.sleep(1)
    else:
        logger.warning("Auto-start: server not ready after 30s, starting anyway")

    logger.info("Auto-start: initializing...")
    await _bot_app.initialize()
    logger.info("Auto-start: starting...")
    await _bot_app.start()
    logger.info("Auto-start: starting polling...")
    await _bot_app.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

    logger.info("✅ Bot is now RUNNING and polling for updates")
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        logger.info("Shutdown requested...")
        await _bot_app.updater.stop()
        await _bot_app.stop()
        await _bot_app.shutdown()
        _bot_app = None
        logger.info("Telegram Bot stopped")


if __name__ == "__main__":
    main()
