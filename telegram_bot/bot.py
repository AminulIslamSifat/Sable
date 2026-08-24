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
import json
import logging
import sys
from pathlib import Path
from typing import Any

from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
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
                 "error", "skills", "paused")

    def __init__(self) -> None:
        self.answer: str = ""
        self.thinking: str = ""
        self.ask_user_payload: dict | None = None
        self.permission_request: dict | None = None
        self.error: str | None = None
        self.skills: list[dict] = []
        self.paused: bool = False  # True when ask_user or permission_request stops the stream


async def stream_chat_events(
    chat_id: str,
    message: str,
    *,
    model: str | None = None,
    on_status: Any = None,
) -> StreamResult:
    """Send message to Sable and consume SSE stream with full event handling.

    Args:
        chat_id: Sable chat session ID.
        message: User message text.
        model: Optional model override.
        on_status: Async callback(status_text: str) for live status updates.

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

    print(f"[TG BOT] STREAM → POST {url} chat_id={chat_id} msg={message[:80]!r}")
    result = StreamResult()
    answer_parts: list[str] = []
    thinking_parts: list[str] = []
    current_skill_name: str | None = None

    async with aiohttp.ClientSession() as session:
        async with session.post(
            url, json=payload, headers=headers,
            timeout=aiohttp.ClientTimeout(total=_STREAM_TIMEOUT),
        ) as resp:
            print(f"[TG BOT] STREAM ← status={resp.status}")
            if resp.status != 200:
                err = await resp.text()
                print(f"[TG BOT] STREAM ERROR: {err[:200]}")
                result.error = f"Server error ({resp.status}): {err[:500]}"
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
                        answer_parts.append(event.get("text", ""))

                    elif etype == "thinking":
                        thinking_parts.append(event.get("text", ""))

                    elif etype == "skill_start":
                        name = event.get("name", "unknown")
                        current_skill_name = name
                        result.skills.append(event)
                        if on_status:
                            await on_status(f"⚡ Running {name}...")

                    elif etype == "skill_output":
                        result.skills.append(event)
                        # ask_user: parse JSON payload for inline keyboard
                        if event.get("name") == "ask_user":
                            try:
                                result.ask_user_payload = json.loads(event.get("text", "{}"))
                            except (json.JSONDecodeError, TypeError):
                                pass
                        elif on_status and event.get("text"):
                            text_preview = event["text"][:100]
                            await on_status(f"⚡ {current_skill_name or 'skill'}: {text_preview}")

                    elif etype == "skill_end":
                        result.skills.append(event)
                        ok = event.get("ok", False)
                        name = event.get("name", "unknown")
                        icon = "✅" if ok else "❌"
                        if on_status:
                            await on_status(f"{icon} {name} finished")
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
                            await on_status(f"📡 {msg}")

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
    print(f"[TG BOT] /start from user={user.id if user else 'unknown'} name={user.first_name if user else '?'}")
    if not user or not is_user_allowed(user.id):
        print(f"[TG BOT] /start REJECTED — user {user.id if user else '?'} not in allowed list")
        await update.message.reply_text("⛔ Unauthorized.")
        return

    chat_id = get_chat_id_for_user(user.id)
    if chat_id:
        await update.message.reply_text(
            f"👋 Welcome back! You have an active session.\n"
            f"Send me a message to chat with Sable.\n\n"
            f"Commands:\n"
            f"/new — Start a new chat\n"
            f"/reset — Clear current session\n"
            f"/status — Check connection\n"
            f"/help — Show help"
        )
    else:
        await update.message.reply_text(
            f"🤖 Hi {user.first_name}! I'm Sable's Telegram bot.\n"
            f"Send me a message to start chatting.\n\n"
            f"Commands:\n"
            f"/new — Start a new chat\n"
            f"/status — Check connection\n"
            f"/help — Show help"
        )


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /new — create fresh chat session."""
    user = update.effective_user
    print(f"[TG BOT] /new from user={user.id if user else 'unknown'}")
    if not user or not is_user_allowed(user.id):
        print(f"[TG BOT] /new REJECTED — user {user.id if user else '?'} not allowed")
        return

    msg = await update.message.reply_text("⏳ Creating new chat...")
    chat_id = await create_new_chat()
    if chat_id:
        set_chat_id_for_user(user.id, chat_id)
        await msg.edit_text(f"✅ New chat started!\nChat ID: `{chat_id[:8]}...`", parse_mode="Markdown")
    else:
        await msg.edit_text("❌ Failed to create chat. Is Sable server running?")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /reset — clear session mapping."""
    user = update.effective_user
    print(f"[TG BOT] /reset from user={user.id if user else 'unknown'}")
    if not user or not is_user_allowed(user.id):
        print(f"[TG BOT] /reset REJECTED — user {user.id if user else '?'} not allowed")
        return
    clear_chat_for_user(user.id)
    await update.message.reply_text("🔄 Session cleared. Send /new or just message me to start fresh.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status — check Sable server connectivity."""
    user = update.effective_user
    print(f"[TG BOT] /status from user={user.id if user else 'unknown'}")
    if not user or not is_user_allowed(user.id):
        print(f"[TG BOT] /status REJECTED — user {user.id if user else '?'} not allowed")
        return

    msg = await update.message.reply_text("⏳ Checking...")
    try:
        status, body = await _sable_request("GET", "/api/models", timeout=10)
        if status == 200:
            models = body.get("models", []) if isinstance(body, dict) else []
            chat_id = get_chat_id_for_user(user.id)
            session_info = f"Active chat: `{chat_id[:8]}...`" if chat_id else "No active chat"
            await msg.edit_text(
                f"✅ Sable server connected\n"
                f"📡 {get_server_url()}\n"
                f"🤖 {len(models)} models available\n"
                f"💬 {session_info}",
                parse_mode="Markdown",
            )
        else:
            await msg.edit_text(f"❌ Server returned {status}: {str(body)[:200]}")
    except Exception as e:
        await msg.edit_text(f"❌ Cannot reach Sable server: {e}")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help."""
    user = update.effective_user
    print(f"[TG BOT] /help from user={user.id if user else 'unknown'}")
    await update.message.reply_text(
        "🤖 *Sable Telegram Bot*\n\n"
        "Just send me a message and I'll forward it to Sable AI.\n\n"
        "*Commands:*\n"
        "/start — Welcome & info\n"
        "/new — Start a new chat session\n"
        "/reset — Clear current session\n"
        "/status — Check server connection\n"
        "/help — This message\n\n"
        "*Interactive Features:*\n"
        "• 🔒 Permission requests → Approve/Deny buttons\n"
        "• ❓ Ask User questions → Option buttons\n"
        "• ⚡ Tool execution → Live status updates\n"
        "• 💭 Thinking → Shown in italic prefix",
        parse_mode="Markdown",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming text messages — forward to Sable and relay response.

    Supports full interactive loop:
    - Normal responses → chunked text messages
    - ask_user → inline keyboard with options
    - permission_request → approve/session/deny buttons
    - Tool execution → live status message updates
    """
    user = update.effective_user
    print(f"[TG BOT] MESSAGE from user={user.id if user else 'unknown'} name={user.first_name if user else '?'}")
    if not user or not is_user_allowed(user.id):
        print(f"[TG BOT] MESSAGE REJECTED — user {user.id if user else '?'} not allowed")
        return

    text = update.message.text
    print(f"[TG BOT] MESSAGE TEXT: {text!r}")
    if not text or not text.strip():
        print("[TG BOT] MESSAGE EMPTY — skipping")
        return

    # Get or create chat session
    chat_id = get_chat_id_for_user(user.id)
    if not chat_id:
        chat_id = await create_new_chat()
        if not chat_id:
            await update.message.reply_text("❌ Could not create chat session. Is Sable running?")
            return
        set_chat_id_for_user(user.id, chat_id)

    # Create a status message that gets updated during streaming
    status_msg = await update.message.reply_text("💭 Thinking...")

    # Status update callback — edits the status message
    last_status_text = "💭 Thinking..."
    async def update_status(new_text: str) -> None:
        nonlocal last_status_text
        if new_text != last_status_text:
            last_status_text = new_text
            try:
                await status_msg.edit_text(new_text)
            except Exception:
                pass  # Message may have been deleted or unchanged

    # Stream response from Sable
    try:
        result = await stream_chat_events(chat_id, text, on_status=update_status)
    except asyncio.TimeoutError:
        await status_msg.edit_text("⏰ Response timed out (5 min). Try again?")
        return
    except Exception as e:
        logger.exception("Chat stream failed for user %s", user.id)
        await status_msg.edit_text(f"❌ Error: {e}")
        return

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

    # Normal response — delete status message and send answer
    response = result.answer
    if not response:
        if result.thinking:
            response = f"💭 _(thinking only, no final response)_"
        else:
            response = "_(no response)_"

    # Prepend thinking summary if present
    if result.thinking and len(result.thinking) < 500:
        response = f"💭 _{result.thinking[:300]}_\n\n{response}"

    # Delete status message
    try:
        await status_msg.delete()
    except Exception:
        pass

    # Send response (chunked if needed)
    chunks = chunk_message(response)
    for i, chunk_text in enumerate(chunks):
        try:
            await update.message.reply_text(chunk_text, parse_mode="Markdown")
        except Exception:
            # Fallback without markdown
            try:
                await update.message.reply_text(chunk_text)
            except Exception as e:
                logger.warning("Failed to send chunk %d/%d: %s", i + 1, len(chunks), e)


# ── Callback query handlers ──────────────────────────────────────────────────
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard button presses for ask_user and permission_request."""
    query = update.callback_query
    if not query or not query.data:
        return

    user = update.effective_user
    print(f"[TG BOT] CALLBACK from user={user.id if user else 'unknown'} data={query.data!r}")
    if not user or not is_user_allowed(user.id):
        print(f"[TG BOT] CALLBACK REJECTED — user {user.id if user else '?'} not allowed")
        await query.answer("⛔ Unauthorized", show_alert=True)
        return

    data = query.data

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
            # Simulate a user message by calling handle_message logic
            # We need to send the option as a regular message
            status_msg = await query.message.reply_text("💭 Processing selection...")

            last_status = "💭 Processing selection..."
            async def update_status(new_text: str) -> None:
                nonlocal last_status
                if new_text != last_status:
                    last_status = new_text
                    try:
                        await status_msg.edit_text(new_text)
                    except Exception:
                        pass

            try:
                result = await stream_chat_events(chat_id, option_text, on_status=update_status)
            except Exception as e:
                await status_msg.edit_text(f"❌ Error: {e}")
                return

            # Process result same as handle_message
            await _send_stream_result(query.message, result, context, chat_id)

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
            status_msg = await query.message.reply_text("💭 Continuing...")

            last_status = "💭 Continuing..."
            async def update_status(new_text: str) -> None:
                nonlocal last_status
                if new_text != last_status:
                    last_status = new_text
                    try:
                        await status_msg.edit_text(new_text)
                    except Exception:
                        pass

            try:
                # Send empty-ish continuation message
                result = await stream_chat_events(
                    chat_id, "continue", on_status=update_status
                )
            except Exception as e:
                await status_msg.edit_text(f"❌ Error continuing: {e}")
                return

            await _send_stream_result(query.message, result, context, chat_id)

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
        response = "_(no response)_"

    if result.thinking and len(result.thinking) < 500:
        response = f"💭 _{result.thinking[:300]}_\n\n{response}"

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
def register_handlers(app: Application) -> None:
    """Register all bot handlers on an Application instance."""
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
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

    print(f"[TG BOT] Auto-start: building application with token={bot_token[:6]}...{bot_token[-4:]}")
    _bot_app = Application.builder().token(bot_token).build()
    register_handlers(_bot_app)
    _bot_app.post_init = _post_init

    print(f"[TG BOT] Auto-start: server={get_server_url()}, allowed={get_allowed_users() or 'ALL'}")
    logger.info("Starting Sable Telegram Bot (background polling mode)...")
    logger.info("Server: %s", get_server_url())
    logger.info("Allowed users: %s", get_allowed_users() or "ALL")

    print("[TG BOT] Auto-start: initializing...")
    await _bot_app.initialize()
    print("[TG BOT] Auto-start: starting...")
    await _bot_app.start()
    print("[TG BOT] Auto-start: starting polling...")
    await _bot_app.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

    print("[TG BOT] ✅ Bot is now RUNNING and polling for updates")
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        print("[TG BOT] Shutdown requested...")
        logger.info("Telegram Bot shutting down...")
        await _bot_app.updater.stop()
        await _bot_app.stop()
        await _bot_app.shutdown()
        _bot_app = None
        logger.info("Telegram Bot stopped")


if __name__ == "__main__":
    main()
