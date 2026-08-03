"""Qwen Engine Configuration — Endpoints, Models, and Default Constants."""

import os
from pathlib import Path

# Project root (two levels up from this file: engine/config.py → engine/ → project root)
_ROOT = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------
# Server bind settings — single source of truth for the FastAPI/uvicorn app.
# Override with SABLE_HOST / SABLE_PORT environment variables when needed.
# --------------------------------------------------------------------------
HOST = os.getenv("SABLE_HOST", "0.0.0.0")
PORT = int(os.getenv("SABLE_PORT", "61770"))

# --------------------------------------------------------------------------
# Runtime data paths — single source of truth used by server.py and any
# other module that needs these files.
# --------------------------------------------------------------------------
BRAIN_DIR = _ROOT / "Brain"
MEMORY_PATH = BRAIN_DIR / "Memory.json"
PROTECTED_PATH = BRAIN_DIR / "Protected.json"
MEMORY_SEARCH_SETTINGS_PATH = _ROOT / "system/memory_search_settings.json"
AGENT_CONFIG_PATH = _ROOT / "system/agent_config.json"

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

# Each model carries its own list of selectable "thinking modes" — some
# models only support one mode (e.g. qwen3.8-max-preview is Thinking-only),
# others support several (qwen3.7-max: Fast/Thinking, qwen3.7-plus:
# Fast/Auto/Thinking). Each thinking mode entry maps directly onto the
# feature_config fields the upstream API expects. Add/remove model or mode
# entries here to control what's selectable in the UI.
MODELS = [
    {
        "id": "qwen3.8-max-preview",
        "label": "Qwen3.8 Max Preview",
        "capabilities": {"image": True, "video": False, "document": False, "audio": False},
        "thinking_modes": [
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