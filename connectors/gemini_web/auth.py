"""Per-account Gemini web auth store.

Mirrors the deepseek/zai token stores: which accounts hold valid Google
auth, plus the cookie jar itself, persisted to disk so we can fall back
when the ACTIVE account has no Gemini login.

Cookies normally live encrypted in Chromium's store — only a live browser
context can read them. We snapshot them here on first successful live read
so subsequent calls survive even if the active account is switched away.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

logger = logging.getLogger("sable.gemini_web.auth")

_SYSTEM_DIR = Path(__file__).resolve().parent.parent.parent / "system"
STORE_PATH = _SYSTEM_DIR / ".gemini_web_auth.json"

# Consider a disk snapshot stale after this long; prefer a live re-read.
SNAPSHOT_TTL = 7 * 24 * 3600

# The cookies that actually authenticate a Gemini web session.
_AUTH_COOKIES = ("SAPISID", "__Secure-1PSID", "__Secure-3PSID")


def has_google_auth(jar: dict[str, str] | None) -> bool:
    """True if the jar carries at least one real Google auth cookie."""
    if not jar:
        return False
    return any(jar.get(k) for k in _AUTH_COOKIES)


def _load_store() -> dict:
    if not STORE_PATH.exists():
        return {}
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_store(store: dict) -> None:
    try:
        fd, tmp = tempfile.mkstemp(dir=str(STORE_PATH.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(store, f, indent=1)
            os.replace(tmp, str(STORE_PATH))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as exc:
        logger.warning("could not save gemini auth store: %s", exc)


def _key(user_data_dir: str) -> str:
    return Path(user_data_dir).name


def save_gemini_auth(user_data_dir: str, jar: dict[str, str]) -> None:
    """Snapshot a valid cookie jar for an account."""
    if not has_google_auth(jar):
        return
    store = _load_store()
    store[_key(user_data_dir)] = {"cookies": jar, "saved_at": time.time()}
    _save_store(store)


def load_gemini_auth_for(user_data_dir: str) -> dict[str, str] | None:
    """Return the snapshot for a specific account, if fresh enough."""
    entry = _load_store().get(_key(user_data_dir))
    if not isinstance(entry, dict):
        return None
    jar = entry.get("cookies")
    return jar if has_google_auth(jar) else None


def load_best_gemini_auth() -> tuple[str, dict[str, str]] | None:
    """Return (account_key, jar) for the most recently saved valid account."""
    store = _load_store()
    best: tuple[str, dict, float] | None = None
    for acct, entry in store.items():
        if not isinstance(entry, dict):
            continue
        jar = entry.get("cookies")
        if not has_google_auth(jar):
            continue
        saved = float(entry.get("saved_at") or 0)
        if best is None or saved > best[2]:
            best = (acct, jar, saved)
    if best is None:
        return None
    return best[0], best[1]


def list_valid_accounts() -> list[str]:
    """Account keys that currently hold a valid snapshot."""
    out = []
    for acct, entry in _load_store().items():
        if isinstance(entry, dict) and has_google_auth(entry.get("cookies")):
            out.append(acct)
    return sorted(out)


def snapshot_age(user_data_dir: str) -> float | None:
    """Seconds since the snapshot was saved, or None if absent."""
    entry = _load_store().get(_key(user_data_dir))
    if not isinstance(entry, dict) or not entry.get("saved_at"):
        return None
    return time.time() - float(entry["saved_at"])
