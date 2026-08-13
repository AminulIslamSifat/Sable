"""Qwen Engine Configuration — Endpoints, Models, and Default Constants."""

import logging
import os
from pathlib import Path

logger = logging.getLogger("sable")

# Project root (two levels up from this file: engine/config.py → engine/ → project root)
_ROOT = Path(__file__).resolve().parent.parent

# Persistent storage root — survives SSD tree wipes/rebuilds.
# Sessions, tokens, and auth state MUST live here, not under _ROOT.
_PERSISTENT_OVERRIDE = os.getenv("SABLE_PERSISTENT_ROOT")
PERSISTENT_ROOT = (
    Path(_PERSISTENT_OVERRIDE).resolve()
    if _PERSISTENT_OVERRIDE
    else Path.home() / "hdd" / "projects" / "Sable"
)

# --------------------------------------------------------------------------
# Server bind settings — single source of truth for the FastAPI/uvicorn app.
# Override with SABLE_HOST / SABLE_PORT environment variables when needed.
# --------------------------------------------------------------------------
HOST = os.getenv("SABLE_HOST", "0.0.0.0")
_DEFAULT_PORT = "61771" if "/home/sifat/hdd/" in str(_ROOT) else "61770"
PORT = int(os.getenv("SABLE_PORT", _DEFAULT_PORT))

# --------------------------------------------------------------------------
# Runtime data paths — single source of truth used by server.py and any
# other module that needs these files.
# --------------------------------------------------------------------------
BRAIN_DIR = _ROOT / "Brain"
MEMORY_PATH = BRAIN_DIR / "Memory.json"
PROTECTED_PATH = BRAIN_DIR / "Protected.json"
PROCEDURAL_PATH = BRAIN_DIR / "Procedural.json"
PERSONALITY_PATH = BRAIN_DIR / "user_personality.json"
INSTRUCTION_DIR = _ROOT / "instruction"
PERSONAL_PATH = INSTRUCTION_DIR / "personal.md"
MEMORY_SEARCH_SETTINGS_PATH = _ROOT / "system/memory_search_settings.json"
AGENT_CONFIG_PATH = _ROOT / "system/agent_config.json"

# Output directories — where generated content lands
OUTPUT_ROOT = _ROOT / "output"
RESEARCH_DIR = OUTPUT_ROOT / "research"
NOTES_DIR = OUTPUT_ROOT / "notes"
AGENT_OUTPUT_DIR = OUTPUT_ROOT / "agent"
ASSETS_DIR = OUTPUT_ROOT / "assets"

# User-created skills (managed via memory consolidation)
SKILLS_JSON_PATH = BRAIN_DIR / "skills.json"

# Maximum prompt length (chars) before memory vectorization is skipped entirely.
# Prevents RAM spikes when huge messages are sent. Configurable via web settings.
MEMORY_SEARCH_MAX_PROMPT_CHARS = 20000

# Browser profile directories — single source of truth for all browser data paths.
# All profiles live under system/ to keep the project root clean.
_SYSTEM = _ROOT / "system"
BROWSER_DATA_DIR = _SYSTEM / "browser-data"
BROWSER_SCRAPER_DATA_DIR = _SYSTEM / "browser-scraper-data"
BROWSER_AUTOMATION_DATA_DIR = _SYSTEM / "automation-browser-data"

URL = "https://chat.qwen.ai/api/v2/chat/completions"
NEW_CHAT_URL = "https://chat.qwen.ai/api/v2/chats/new"
STOP_URL = "https://chat.qwen.ai/api/v2/chat/completions/stop"

# Each model carries its own list of selectable "thinking modes" — some
# others support several (qwen3.7-max: Fast/Thinking, qwen3.7-plus:
# Fast/Auto/Thinking). Each thinking mode entry maps directly onto the
# feature_config fields the upstream API expects. Add/remove model or mode
# entries here to control what's selectable in the UI.
MODELS = [
    {
        "id": "qwen3.8-max",
        "label": "Qwen3.8 Max",
        "max_session_chars": 3_000_000,
        "capabilities": {"image": True, "video": False, "document": False, "audio": False},
        "thinking_modes": [
            {
                "id": "fast",
                "label": "Fast",
                "thinking_enabled": False,
                "auto_thinking": False,
                "thinking_mode": "Fast",
            },
            {
                "id": "auto",
                "label": "Auto",
                "thinking_enabled": True,
                "auto_thinking": True,
                "thinking_mode": "Auto",
            },
            {
                "id": "thinking",
                "label": "Thinking",
                "thinking_enabled": True,
                "auto_thinking": False,
                "thinking_mode": "Thinking",
            },
        ],
    },
    {
        "id": "qwen3.7-max",
        "label": "Qwen3.7 Max",
        "max_session_chars": 3_000_000,
        "capabilities": {"image": True, "video": False, "document": False, "audio": False},
        "thinking_modes": [
            {
                "id": "fast",
                "label": "Fast",
                "thinking_enabled": False,
                "auto_thinking": False,
                "thinking_mode": "Fast",
            },
            {
                "id": "thinking",
                "label": "Thinking",
                "thinking_enabled": True,
                "auto_thinking": False,
                "thinking_mode": "Thinking",
            },
        ],
    },
    {
        "id": "qwen3.7-plus",
        "label": "Qwen3.7 Plus",
        "max_session_chars": 3_000_000,
        "capabilities": {"image": True, "video": False, "document": False, "audio": False},
        "thinking_modes": [
            {
                "id": "fast",
                "label": "Fast",
                "thinking_enabled": False,
                "auto_thinking": False,
                "thinking_mode": "Fast",
            },
            {
                "id": "auto",
                "label": "Auto",
                "thinking_enabled": True,
                "auto_thinking": True,
                "thinking_mode": "Auto",
            },
            {
                "id": "thinking",
                "label": "Thinking",
                "thinking_enabled": True,
                "auto_thinking": False,
                "thinking_mode": "Thinking",
            },
        ],
    },
    {
        "id": "deepseek-expert",
        "label": "DeepSeek Expert",
        "api_backend": "deepseek",
        "api_model_type": "expert",
        "max_session_chars": 1_000_000,
        "capabilities": {"image": False, "video": False, "document": False, "audio": False},
        "thinking_modes": [
            {
                "id": "fast",
                "label": "Fast",
                "thinking_enabled": False,
                "auto_thinking": False,
                "thinking_mode": "Fast",
            },
            {
                "id": "thinking",
                "label": "Thinking",
                "thinking_enabled": True,
                "auto_thinking": False,
                "thinking_mode": "Thinking",
            },
        ],
    },
    {
        "id": "deepseek-instant",
        "label": "DeepSeek Instant",
        "api_backend": "deepseek",
        "api_model_type": None,
        "max_session_chars": 1_000_000,
        "capabilities": {"image": False, "video": False, "document": False, "audio": False},
        "thinking_modes": [
            {
                "id": "fast",
                "label": "Fast",
                "thinking_enabled": False,
                "auto_thinking": False,
                "thinking_mode": "Fast",
            },
            {
                "id": "thinking",
                "label": "Thinking",
                "thinking_enabled": True,
                "auto_thinking": False,
                "thinking_mode": "Thinking",
            },
        ],
    },
    {
        "id": "deepseek-vision",
        "label": "DeepSeek Vision",
        "api_backend": "deepseek",
        "api_model_type": "vision",
        "max_session_chars": 1_000_000,
        "capabilities": {"image": True, "video": False, "document": False, "audio": False},
        "thinking_modes": [
            {
                "id": "fast",
                "label": "Fast",
                "thinking_enabled": False,
                "auto_thinking": False,
                "thinking_mode": "Fast",
            },
            {
                "id": "thinking",
                "label": "Thinking",
                "thinking_enabled": True,
                "auto_thinking": False,
                "thinking_mode": "Thinking",
            },
        ],
    },
    {
        "id": "gemini-2.5-flash",
        "label": "Gemini 2.5 Flash",
        "api_backend": "gemini",
        "api_model_type": "gemini-2.5-flash",
        "capabilities": {"image": True, "video": False, "document": True, "audio": False},
        "thinking_modes": [
            {"id": "fast", "label": "Fast", "thinking_enabled": False, "auto_thinking": False, "thinking_mode": "Fast"},
            {"id": "low", "label": "Low", "thinking_enabled": True, "auto_thinking": False, "thinking_mode": "Low"},
            {"id": "medium", "label": "Medium", "thinking_enabled": True, "auto_thinking": False, "thinking_mode": "Medium"},
            {"id": "high", "label": "High", "thinking_enabled": True, "auto_thinking": False, "thinking_mode": "High"},
        ],
    },
    {
        "id": "gemini-2.5-pro",
        "label": "Gemini 2.5 Pro",
        "api_backend": "gemini",
        "api_model_type": "gemini-2.5-pro",
        "capabilities": {"image": True, "video": False, "document": True, "audio": False},
        "thinking_modes": [
            {"id": "fast", "label": "Fast", "thinking_enabled": False, "auto_thinking": False, "thinking_mode": "Fast"},
            {"id": "low", "label": "Low", "thinking_enabled": True, "auto_thinking": False, "thinking_mode": "Low"},
            {"id": "medium", "label": "Medium", "thinking_enabled": True, "auto_thinking": False, "thinking_mode": "Medium"},
            {"id": "high", "label": "High", "thinking_enabled": True, "auto_thinking": False, "thinking_mode": "High"},
        ],
    },
]

# Default/current model id — kept for backward compatibility with code that
# imports MODEL directly (chat.py, session.py create_new_chat, etc.)
MODEL = MODELS[0]["id"]

# ---------------------------------------------------------------------------
# Dynamic model registry — user-added models from Providers UI
# ---------------------------------------------------------------------------
_CUSTOM_MODELS_PATH = _SYSTEM / ".custom_models.json"
_HIDDEN_MODELS_PATH = _SYSTEM / ".hidden_models.json"


def _load_hidden_models() -> list[str]:
    """Load list of hidden (user-deleted) static model IDs."""
    if not _HIDDEN_MODELS_PATH.exists():
        return []
    try:
        import json as _json
        data = _json.loads(_HIDDEN_MODELS_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def _save_hidden_models(ids: list[str]) -> None:
    """Persist hidden model IDs."""
    import json as _json
    _HIDDEN_MODELS_PATH.write_text(_json.dumps(ids, indent=2), encoding="utf-8")


def _load_custom_models() -> list[dict]:
    """Load user-added model definitions."""
    if not _CUSTOM_MODELS_PATH.exists():
        return []
    try:
        import json as _json
        data = _json.loads(_CUSTOM_MODELS_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def _save_custom_models(models: list[dict]) -> None:
    """Persist user-added model definitions."""
    import json as _json
    _CUSTOM_MODELS_PATH.write_text(_json.dumps(models, indent=2), encoding="utf-8")


def get_all_models() -> list[dict]:
    """Return static + custom models merged, excluding hidden/deleted ones."""
    hidden = set(_load_hidden_models())
    custom = _load_custom_models()
    custom_ids = {m["id"] for m in custom}
    # Static models: exclude hidden and custom-overridden
    merged = [m for m in MODELS if m["id"] not in custom_ids and m["id"] not in hidden]
    # Custom models: exclude hidden
    merged.extend(m for m in custom if m.get("id") not in hidden)
    return merged


def add_custom_model(model_def: dict) -> None:
    """Add or update a custom model definition."""
    customs = _load_custom_models()
    mid = model_def.get("id", "")
    # Replace if exists, else append
    customs = [m for m in customs if m.get("id") != mid]
    customs.append(model_def)
    _save_custom_models(customs)


def remove_custom_model(model_id: str) -> bool:
    """Remove a custom model. Returns True if removed."""
    customs = _load_custom_models()
    new = [m for m in customs if m.get("id") != model_id]
    if len(new) < len(customs):
        _save_custom_models(new)
        return True
    return False


def get_model_config(model_id: str | None = None) -> dict:
    """Return the model config dict for model_id, falling back to the default MODEL.

    Searches both static MODELS and user-added custom models.
    Falls back to the first entry rather than raising.
    """
    target = model_id or MODEL
    for entry in get_all_models():
        if entry["id"] == target:
            return entry
    return get_all_models()[0]


def get_thinking_mode_config(model_id: str | None = None, thinking_mode_id: str | None = None) -> dict:
    """Return the selected thinking-mode config for a model.

    Falls back to that model's first (default) thinking mode if
    thinking_mode_id is missing or not supported by the model — e.g. the
    client requests "auto" on qwen3.7-max, which doesn't have that mode.
    """
    modes = get_model_config(model_id)["thinking_modes"]
    if thinking_mode_id:
        for mode in modes:
            if mode["id"] == thinking_mode_id:
                return mode
    return modes[0]

# --------------------------------------------------------------------------
# Session tokens — loaded from .session_tokens.json (gitignored) so they
# are never committed to the repository.  Playwright auto-refresh is the
# real authentication mechanism; these are only used as the initial seed.
# --------------------------------------------------------------------------

def _load_session_tokens() -> dict:
    token_file = _ROOT / "system" / ".session_tokens.json"
    if token_file.exists():
        try:
            import json as _json
            return _json.loads(token_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

_SESSION_TOKENS = _load_session_tokens()

COOKIES: str = _SESSION_TOKENS.get("COOKIES", "")
BX_UA: str = _SESSION_TOKENS.get("BX_UA", "")
BX_UMIDTOKEN: str = _SESSION_TOKENS.get("BX_UMIDTOKEN", "")


# --------------------------------------------------------------------------
# Per-account Qwen WAF token store (list-based — tokens accumulate).
# Format: {"browser-data-acc1": [{"cookies": "...", "bx_ua": "...", "bx_umidtoken": "..."}, ...]}
# Tokens don't expire, so we keep all of them and use the most recent.
# --------------------------------------------------------------------------

_QWEN_TOKENS_PATH = _SYSTEM / ".qwen_tokens.json"
_QWEN_MAX_TOKENS_PER_ACCOUNT = 10
_QWEN_LEGACY_MIGRATED = False  # guard so migration only runs once


def _resolve_active_account() -> str:
    """Get the active account name from the browser-data symlink target."""
    symlink = _SYSTEM / "browser-data"
    try:
        target = symlink.resolve()
        return target.name
    except OSError:
        return "browser-data"


def load_qwen_token_store() -> dict[str, list[dict[str, str]]]:
    """Load per-account Qwen WAF token store.

    Returns {account: [{cookies, bx_ua, bx_umidtoken}, ...]}.
    Handles migration from old flat .session_tokens.json format (runs once only).
    """
    global _QWEN_LEGACY_MIGRATED
    import json as _json
    raw: dict = {}
    if _QWEN_TOKENS_PATH.exists():
        try:
            raw = _json.loads(_QWEN_TOKENS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass

    store: dict[str, list[dict[str, str]]] = {}
    for key, val in raw.items():
        if isinstance(val, list):
            store[key] = [v for v in val if isinstance(v, dict) and v.get("cookies")]
        elif isinstance(val, dict) and val.get("cookies"):
            # Migrate old flat format → list
            store[key] = [val]
        else:
            store[key] = []

    # One-time migration from global .session_tokens.json (guarded)
    if not store and not _QWEN_LEGACY_MIGRATED and _SESSION_TOKENS.get("COOKIES"):
        _QWEN_LEGACY_MIGRATED = True
        account = _resolve_active_account()
        entry = {
            "cookies": _SESSION_TOKENS.get("COOKIES", ""),
            "bx_ua": _SESSION_TOKENS.get("BX_UA", ""),
            "bx_umidtoken": _SESSION_TOKENS.get("BX_UMIDTOKEN", ""),
        }
        store[account] = [entry]
        save_qwen_token_store(store)

    return store


def save_qwen_token_store(store: dict[str, list[dict[str, str]]]) -> None:
    """Persist the per-account Qwen WAF token store atomically (write-to-tmp + rename)."""
    import json as _json
    import tempfile
    import os as _os
    try:
        data = _json.dumps(store, indent=2)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(_QWEN_TOKENS_PATH.parent), suffix=".tmp"
        )
        try:
            with _os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
            _os.replace(tmp_path, str(_QWEN_TOKENS_PATH))
        except BaseException:
            try:
                _os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception:
        pass


def get_qwen_tokens_for_account(account: str | None = None) -> dict[str, str] | None:
    """Get the most recent Qwen WAF tokens for an account.

    Returns {cookies, bx_ua, bx_umidtoken} or None if nothing cached.
    """
    store = load_qwen_token_store()
    if not store:
        return None

    def _latest(entries: list[dict[str, str]]) -> dict[str, str] | None:
        valid = [e for e in entries if e.get("cookies")]
        return valid[-1] if valid else None

    if account and account in store:
        tok = _latest(store[account])
        if tok:
            return tok
    active = _resolve_active_account()
    if active in store:
        tok = _latest(store[active])
        if tok:
            return tok
    for entries in store.values():
        tok = _latest(entries)
        if tok:
            return tok
    return None


def save_qwen_tokens_for_account(
    cookies: str,
    bx_ua: str,
    bx_umidtoken: str,
    account: str | None = None,
) -> None:
    """Save Qwen WAF tokens for an account. Replaces any existing entry (1 per account)."""
    if not cookies:
        return
    acct = account or _resolve_active_account()
    store = load_qwen_token_store()
    store[acct] = [{"cookies": cookies, "bx_ua": bx_ua, "bx_umidtoken": bx_umidtoken}]
    save_qwen_token_store(store)



# --------------------------------------------------------------------------
# Qwen account exhaustion tracking (daily quota resets at UTC 00:00)
# --------------------------------------------------------------------------

_QWEN_EXHAUSTION_PATH = _SYSTEM / ".qwen_exhaustion.json"


def _load_exhaustion_store() -> dict[str, dict]:
    import json as _json
    if _QWEN_EXHAUSTION_PATH.exists():
        try:
            return _json.loads(_QWEN_EXHAUSTION_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_exhaustion_store(store: dict[str, dict]) -> None:
    import json as _json
    import tempfile, os as _os
    try:
        data = _json.dumps(store, indent=2)
        fd, tmp = tempfile.mkstemp(dir=str(_QWEN_EXHAUSTION_PATH.parent), suffix=".tmp")
        try:
            with _os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
            _os.replace(tmp, str(_QWEN_EXHAUSTION_PATH))
        except BaseException:
            try:
                _os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception:
        pass


def mark_account_exhausted(account: str) -> None:
    """Mark an account as quota-exhausted with current UTC timestamp."""
    from datetime import datetime, timezone
    store = _load_exhaustion_store()
    store[account] = {
        "exhausted": True,
        "exhausted_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_exhaustion_store(store)
    logger.info("Account %s marked exhausted", account)


def is_account_exhausted(account: str) -> bool:
    """Check if account is exhausted. Auto-resets if exhausted_at is before today's UTC midnight."""
    from datetime import datetime, timezone
    store = _load_exhaustion_store()
    entry = store.get(account)
    if not entry or not entry.get("exhausted"):
        return False
    # Check if quota has reset (new UTC day)
    exhausted_at = entry.get("exhausted_at")
    if exhausted_at:
        try:
            dt = datetime.fromisoformat(exhausted_at)
            now = datetime.now(timezone.utc)
            # If exhausted before today's UTC midnight, quota has reset
            if dt.date() < now.date():
                store[account] = {"exhausted": False, "exhausted_at": None}
                _save_exhaustion_store(store)
                return False
        except (ValueError, TypeError):
            pass
    return True


def get_all_exhaustion_status() -> dict[str, bool]:
    """Return {account_name: is_exhausted} for all tracked accounts."""
    store = _load_exhaustion_store()
    result = {}
    for account in store:
        result[account] = is_account_exhausted(account)
    return result

