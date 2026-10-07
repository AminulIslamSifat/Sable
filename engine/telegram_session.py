"""Auto-healing Telegram session management.

Telegram MTProto **permanently** invalidates an auth key when it is used from
two different IPs / processes at the same time
(``AUTH_KEY_DUPLICATED``), when it is revoked, or when the account is
deactivated. Copying a ``.session`` file between project clones (Sable-exp,
Sable, Sable-core, Sable-cloud, the telegram skill …) is the usual cause: every
clone then carries the *same* auth key and they collide.

Once poisoned, retrying never helps — the only fix is a brand-new key. This
module detects those fatal errors and purges the dead session file so the next
attempt starts clean, with no manual ``rm`` required.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: Exception class names (Telethon) that mean "this auth key is dead forever".
_DEAD_SESSION_CLASSES = frozenset(
    {
        "AuthKeyDuplicatedError",
        "AuthKeyUnregisteredError",
        "SessionRevokedError",
        "UserDeactivatedError",
        "UserDeactivatedBanError",
    }
)

#: Substrings found in the stringified RPC error, as a belt-and-braces check.
_DEAD_SESSION_MARKERS = (
    "AUTH_KEY_DUPLICATED",
    "AUTH_KEY_UNREGISTERED",
    "AUTH_KEY_INVALID",
    "SESSION_REVOKED",
    "USER_DEACTIVATED",
)


def is_dead_session_error(exc: BaseException) -> bool:
    """Return ``True`` if *exc* means the session's auth key can never work again.

    These are unrecoverable — no retry, no reconnect, no waiting. The only
    cure is to delete the ``.session`` file and sign in again.
    """
    if type(exc).__name__ in _DEAD_SESSION_CLASSES:
        return True
    msg = str(exc).upper()
    return any(marker in msg for marker in _DEAD_SESSION_MARKERS)


def purge_session(session_path: str | Path) -> list[Path]:
    """Delete a poisoned Telethon session file and all its SQLite side-files.

    Accepts either the base path (``…/sable_tg``) or the file path
    (``…/sable_tg.session``). Idempotent — safe to call when nothing exists.

    Returns the list of paths that were actually removed.
    """
    base = str(session_path)
    if base.endswith(".session"):
        base = base[: -len(".session")]

    removed: list[Path] = []
    for suffix in (".session", ".session-journal", ".session-wal", ".session-shm"):
        p = Path(base + suffix)
        if not p.exists():
            continue
        try:
            p.unlink()
            removed.append(p)
            logger.warning("Telegram: purged poisoned session file %s", p)
        except OSError as e:  # pragma: no cover - filesystem race
            logger.error("Telegram: could not purge %s: %s", p, e)
    return removed
