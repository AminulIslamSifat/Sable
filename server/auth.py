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