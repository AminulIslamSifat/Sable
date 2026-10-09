from __future__ import annotations

import json
import os
from .config import _AUTH_TOKEN_FILE

# Account sessions live alongside the primary install token. One install can
# hold many live sessions (many browsers / many origins) at once — logging in
# from one browser must NEVER evict another.
_SESSIONS_FILE = _AUTH_TOKEN_FILE.parent / ".sessions.json"


def _load_primary() -> str | None:
    """Load the primary install token from file or SABLE_TOKEN env var.

    Returns None if neither is set — the setup flow will force the user to
    create a password before anything else works.
    """
    if _AUTH_TOKEN_FILE.exists():
        token = _AUTH_TOKEN_FILE.read_text(encoding="utf-8").strip()
        if token:
            return token
    env_token = os.environ.get("SABLE_TOKEN", "").strip()
    return env_token or None


def _load_sessions() -> dict[str, dict]:
    try:
        if _SESSIONS_FILE.is_file():
            data = json.loads(_SESSIONS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): (v if isinstance(v, dict) else {}) for k, v in data.items() if k}
    except Exception:
        pass
    return {}


AUTH_TOKEN: str | None = _load_primary()
VALID_TOKENS: dict[str, dict] = _load_sessions()


def _persist_sessions() -> None:
    try:
        _SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SESSIONS_FILE.write_text(json.dumps(VALID_TOKENS, indent=2), encoding="utf-8")
    except OSError:
        pass


def has_any_token() -> bool:
    """True if this install has at least one usable credential."""
    return bool(AUTH_TOKEN) or bool(VALID_TOKENS)


def is_valid_token(token: str | None) -> bool:
    """True if the presented token belongs to ANY live session."""
    if not token:
        return False
    if AUTH_TOKEN and token == AUTH_TOKEN:
        return True
    return token in VALID_TOKENS


def set_auth_token(token: str, email: str = "") -> None:
    """Register a session token WITHOUT invalidating existing sessions.

    On a fresh install the first token also becomes the primary install token
    (persisted to the legacy file) so setup/change-password/terminal keep
    working unchanged. Every later token is purely additive.
    """
    global AUTH_TOKEN
    token = (token or "").strip()
    if not token:
        return
    if not AUTH_TOKEN:
        AUTH_TOKEN = token
        try:
            _AUTH_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            _AUTH_TOKEN_FILE.write_text(token, encoding="utf-8")
        except OSError:
            pass
    VALID_TOKENS[token] = {"email": email}
    _persist_sessions()


def revoke_token(token: str | None) -> None:
    """Revoke a single session. Other sessions stay logged in."""
    global AUTH_TOKEN
    token = (token or "").strip()
    if not token:
        return
    VALID_TOKENS.pop(token, None)
    _persist_sessions()
    if token == AUTH_TOKEN:
        # Only the primary was this browser's credential — drop it too, unless
        # other sessions remain (then keep them working; nothing to fall back to
        # for the primary, so it's simply gone).
        AUTH_TOKEN = None
        try:
            _AUTH_TOKEN_FILE.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def clear_auth_token() -> None:
    """Backward-compat full logout: revoke every session on this install."""
    global AUTH_TOKEN
    VALID_TOKENS.clear()
    _persist_sessions()
    try:
        _AUTH_TOKEN_FILE.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass
    AUTH_TOKEN = None
