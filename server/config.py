from __future__ import annotations

from pathlib import Path
from engine.config import (
    MODELS,
    MEMORY_PATH as _MEMORY_PATH,
    PROTECTED_PATH as _PROTECTED_PATH,
    PROCEDURAL_PATH as _PROCEDURAL_PATH,
    PERSONALITY_PATH as _PERSONALITY_PATH,
    PERSONAL_PATH as _PERSONAL_PATH,
    MEMORY_SEARCH_SETTINGS_PATH as _MEMORY_SEARCH_SETTINGS,
    MEMORY_SEARCH_MAX_PROMPT_CHARS as _DEFAULT_MAX_PROMPT_CHARS,
    get_model_config,
    BROWSER_DATA_DIR,
    BROWSER_SCRAPER_DATA_DIR,
    BROWSER_AUTOMATION_DATA_DIR,
    HOST,
    PORT,
)

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"
INDEX_FILE = WEB_DIR / "index.html"
DB_PATH = BASE_DIR / "system/sable.db"
UPLOAD_DIR = BASE_DIR / "system/uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
_AUTH_TOKEN_FILE = Path(__file__).resolve().parent.parent / "system/.auth_token"
AUTH_EXEMPT_PREFIXES = ("/api/login", "/api/health", "/static/", "/system/uploads/", "/assets/", "/api/settings/accounts/create", "/api/setup/")

MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0
SKILL_ROUND_WARN_THRESHOLD = 15

# ── Typewriter animation (frontend) ──
# Chars revealed per tick and interval in ms. Lower ms / higher chars = faster.
TYPEWRITER_CHARS_PER_TICK = 3
TYPEWRITER_TICK_MS = 12

DEEPSEEK_MODELS = [
    {"id": "default", "label": "Instant", "thinking_modes": [
        {"id": "deepthink", "label": "DeepThink"},
        {"id": "fast", "label": "Fast"},
    ]},
    {"id": "expert", "label": "Expert", "thinking_modes": [
        {"id": "deepthink", "label": "DeepThink"},
        {"id": "fast", "label": "Fast"},
    ]},
    {"id": "vision", "label": "Vision", "thinking_modes": [
        {"id": "deepthink", "label": "DeepThink"},
        {"id": "fast", "label": "Fast"},
    ]},
]

_SYSTEM_DIR = BROWSER_DATA_DIR.parent
_ACTIVE_PROFILE_LINK = _SYSTEM_DIR / "browser-data"

_BROWSER_PROFILES: dict[str, tuple[Path, Path]] = {
    "api": (BROWSER_DATA_DIR, BROWSER_DATA_DIR.parent / (BROWSER_DATA_DIR.name + ".bak")),
    "scraper": (BROWSER_SCRAPER_DATA_DIR, BROWSER_SCRAPER_DATA_DIR.parent / (BROWSER_SCRAPER_DATA_DIR.name + ".bak")),
    "automation": (BROWSER_AUTOMATION_DATA_DIR, BROWSER_AUTOMATION_DATA_DIR.parent / (BROWSER_AUTOMATION_DATA_DIR.name + ".bak")),
}