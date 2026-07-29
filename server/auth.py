from __future__ import annotations

import os
from pathlib import Path
from .config import _AUTH_TOKEN_FILE

def _load_auth_token() -> str:
    if _AUTH_TOKEN_FILE.exists():
        token = _AUTH_TOKEN_FILE.read_text(encoding="utf-8").strip()
        if token:
            return token
    return os.environ.get("SABLE_TOKEN", "sable")

AUTH_TOKEN = _load_auth_token()