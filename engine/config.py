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
PORT = int(os.getenv("SABLE_PORT", "61771"))

# --------------------------------------------------------------------------
# Runtime data paths — single source of truth used by server.py and any
# other module that needs these files.
# --------------------------------------------------------------------------
BRAIN_DIR = _ROOT / "Brain"
MEMORY_PATH = BRAIN_DIR / "Memory.json"
PROTECTED_PATH = BRAIN_DIR / "Protected.json"
MEMORY_SEARCH_SETTINGS_PATH = _ROOT / "system/memory_search_settings.json"

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
]

# Default/current model id — kept for backward compatibility with code that
# imports MODEL directly (chat.py, session.py create_new_chat, etc.)
MODEL = MODELS[0]["id"]


def get_model_config(model_id: str | None = None) -> dict:
    """Return the model config dict for model_id, falling back to the default MODEL.

    If model_id isn't found in MODELS, falls back to the first entry rather
    than raising, so an unrecognized/legacy model string doesn't crash payload
    building — it just won't get thinking mode toggled correctly.
    """
    target = model_id or MODEL
    for entry in MODELS:
        if entry["id"] == target:
            return entry
    return MODELS[0]


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