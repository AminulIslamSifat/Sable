from __future__ import annotations

import os
from .config import _AUTH_TOKEN_FILE

def _load_auth_token() -> str | None:
    """Load auth token from file or SABLE_TOKEN env var.

    Returns None if neither is set — the setup flow will force
    the user to create a password before anything else works.
    """
    if _AUTH_TOKEN_FILE.exists():
        token = _AUTH_TOKEN_FILE.read_text(encoding="utf-8").strip()
        if token:
            return token
    env_token = os.environ.get("SABLE_TOKEN", "").strip()
    return env_token or None

AUTH_TOKEN: str | None = _load_auth_token()


def set_auth_token(token: str) -> None:
    """Persist + activate a session token (called after bridge login/register)."""
    global AUTH_TOKEN
    _AUTH_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _AUTH_TOKEN_FILE.write_text(token, encoding="utf-8")
    AUTH_TOKEN = token


def clear_auth_token() -> None:
    """Drop the local session token (logout)."""
    global AUTH_TOKEN
    try:
        _AUTH_TOKEN_FILE.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass
    AUTH_TOKEN = None