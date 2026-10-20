"""Lock-free Chromium cookie reader for Sable.

Reads and decrypts cookies straight from a profile's on-disk SQLite DB,
WITHOUT launching a browser — so it never conflicts with a profile that a
user (or another Sable process) has open.

Helium (this box's browser) uses the standard Chromium Linux scheme:
  - encrypted_value = b"v10" + IV(16) + AES-128-CBC(ciphertext)
  - key = PBKDF2-HMAC-SHA1(password, b"saltysalt", 1, 16)
  - IV  = 16 spaces
  - password = the hardcoded fallback b"peanuts" (Helium does not use the
    GNOME keyring for these managed profiles — verified empirically).

Modern Chromium prefixes the decrypted plaintext with the LAST 16 BYTES of
SHA256(host_key) (a domain-binding tag), sometimes followed by a stray `"`.
We strip that so callers get the raw cookie value.

If a profile ever DOES use the keyring, we fall back to
`secret-tool lookup application <app>`.
"""
from __future__ import annotations

import glob
import hashlib
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable

from Crypto.Cipher import AES

logger = logging.getLogger("sable.cookie_reader")

# Chromium's fixed PBKDF2 salt and IV for the Linux "v10" scheme.
_SALT = b"saltysalt"
_IV = b" " * 16

# Helium's effective password for Sable-managed profiles. Tried first; if a
# profile ever uses the real keyring, `_keyring_password` is used instead.
_FALLBACK_PASSWORDS = (b"peanuts",)

_KEYRING_APPS = ("chrome", "chromium", "helium", "net.imput.helium")


def _keyring_password() -> bytes | None:
    """Best-effort read of the browser's keyring secret via secret-tool."""
    for app in _KEYRING_APPS:
        try:
            out = subprocess.check_output(
                ["secret-tool", "lookup", "application", app],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).strip()
            if out:
                return out.encode()
        except Exception:
            continue
    return None


def _derive_key(password: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha1", password, _SALT, 1, 16)


def _decrypt_v10(enc: bytes, key: bytes) -> str | None:
    """Decrypt a v10/v11-prefixed Chromium cookie value. Returns None on failure.

    ``v10`` and ``v11`` share the exact same layout — b"v1x" + IV(16) +
    AES-CBC(ciphertext) — and differ only in which password the key comes
    from (hardcoded fallback vs OS keyring). The caller picks the key; this
    function just needs to accept either prefix.

    Modern Chromium prefixes the plaintext with the LAST 16 BYTES of
    SHA256(host_key) (a domain-binding tag). We strip that, plus any stray
    wrapping quote, so callers get the raw cookie value.
    """
    if not enc or enc[:3] not in (b"v10", b"v11") or len(enc) < 3 + 16:
        return None
    iv = enc[3:19]
    ct = enc[19:]
    try:
        pt = AES.new(key, AES.MODE_CBC, iv).decrypt(ct)
    except Exception:
        return None
    # Strip PKCS7 padding.
    pad = pt[-1]
    if 0 < pad <= 16 and pad <= len(pt):
        pt = pt[:-pad]

    def _clean(b: bytes) -> str | None:
        s = b.decode("utf-8", "replace")
        s = s.strip('"')
        while s and not s[0].isprintable():
            s = s[1:]
        while s and not s[-1].isprintable():
            s = s[:-1]
        return s or None

    # Try the raw plaintext, then with 16/32-byte domain tags stripped; pick
    # the candidate with the fewest replacement chars (best decode).
    candidates = [pt]
    for prefix_len in (16, 32):
        if len(pt) > prefix_len:
            candidates.append(pt[prefix_len:])

    best: str | None = None
    best_bad = 10**9
    for cand in candidates:
        s = _clean(cand)
        if not s:
            continue
        bad = s.count("\ufffd")
        if bad < best_bad:
            best_bad, best = bad, s
            if bad == 0:
                break
    return best


def _candidate_keys() -> list[bytes]:
    keys = [_derive_key(p) for p in _FALLBACK_PASSWORDS]
    kp = _keyring_password()
    if kp:
        keys.append(_derive_key(kp))
    return keys


def _keyring_keys() -> list[bytes]:
    """Keys derived from the OS keyring password only (the ``v11`` scheme).

    ``v11`` uses the SAME ``v11`` + IV(16) + AES-CBC layout as ``v10`` but the
    AES key comes from the real keyring secret rather than the hardcoded
    ``peanuts`` fallback. A profile that has opted into keyring encryption
    stores every cookie this way, so ``peanuts`` can never open it.
    """
    kp = _keyring_password()
    return [_derive_key(kp)] if kp else []


def _cookie_db_paths(user_data_dir: str) -> list[str]:
    base = Path(user_data_dir)
    hits: list[str] = []
    for pat in ("Default/Cookies", "Cookies", "*/Cookies"):
        hits.extend(glob.glob(str(base / pat)))
    seen: set[str] = set()
    out: list[str] = []
    for h in hits:
        if h not in seen and os.path.isfile(h):
            seen.add(h)
            out.append(h)
    return out


def read_cookies(
    user_data_dir: str,
    names: Iterable[str] | None = None,
    host_contains: str | None = None,
) -> dict[str, str]:
    """Read cookies from a profile's on-disk DB without any browser lock.

    Args:
        user_data_dir: Profile directory (the one holding `Default/Cookies`).
        names: If given, only return cookies with these names.
        host_contains: If given, only return cookies whose host_key contains it.

    Returns:
        {cookie_name: decrypted_value}. Empty dict if nothing readable.
    """
    result: dict[str, str] = {}
    v10_keys: list[bytes] | None = None
    v11_keys: list[bytes] | None = None
    want = set(names) if names else None

    for db_path in _cookie_db_paths(user_data_dir):
        tmp = tempfile.mktemp(prefix="sable_cookies_", suffix=".db")
        rows: list = []
        try:
            shutil.copy2(db_path, tmp)
            for suffix in ("-wal", "-shm"):
                if os.path.isfile(db_path + suffix):
                    try:
                        shutil.copy2(db_path + suffix, tmp + suffix)
                    except Exception:
                        pass
            con = sqlite3.connect(tmp)
            try:
                rows = con.execute(
                    "SELECT host_key, name, value, encrypted_value FROM cookies"
                ).fetchall()
            finally:
                con.close()
        except Exception:
            continue
        finally:
            for p in (tmp, tmp + "-wal", tmp + "-shm"):
                try:
                    os.unlink(p)
                except OSError:
                    pass

        for host_key, name, value, enc in rows:
            if want is not None and name not in want:
                continue
            if host_contains is not None and host_contains not in (host_key or ""):
                continue
            if result.get(name):
                continue
            if value:
                result[name] = value
                continue
            if enc:
                prefix = enc[:3]
                if prefix in (b"v10", b"v11"):
                    # v10 -> key from the hardcoded fallback password.
                    # v11 -> key from the OS keyring secret. Same cipher layout.
                    if prefix == b"v10":
                        if v10_keys is None:
                            v10_keys = _candidate_keys()
                        keys = v10_keys
                    else:
                        if v11_keys is None:
                            v11_keys = _keyring_keys()
                        keys = v11_keys
                    for key in keys:
                        dec = _decrypt_v10(enc, key)
                        # _decrypt_v10 returns the candidate with the FEWEST
                        # replacement chars, which for a wrong key is garbage
                        # rather than None. Reject lossy output outright: a
                        # mojibake value is worse than a missing one, because
                        # callers send it and get an opaque 401.
                        if dec and "\ufffd" not in dec:
                            result[name] = dec
                            break
                else:
                    # Plain (unencrypted) rows are rare but legal — Chromium
                    # writes `value` instead, which is handled above. An
                    # unknown prefix means a scheme we cannot read; skip it
                    # rather than shipping ciphertext as a cookie value.
                    logger.debug(
                        "cookie %r has unknown encryption prefix %r — skipped",
                        name, prefix)
    return result


def read_mimo_cookies(user_data_dir: str) -> dict[str, str]:
    """Convenience: MiMo auth cookies for a profile, lock-free."""
    return read_cookies(
        user_data_dir,
        names=("xiaomichatbot_serviceToken", "userId", "xiaomichatbot_ph"),
        host_contains="xiaomimimo.com",
    )


def read_mistral_web_cookies(user_data_dir: str) -> dict[str, str]:
    """Convenience: Mistral (chat.mistral.ai) auth cookies, lock-free.

    Auth is an Ory Kratos session. The session cookie name is dynamic
    (``ory_session_<suffix>``), so we read every mistral.ai cookie and keep the
    ones the API needs: the ory session + continuity + csrf token, plus the
    anonymous-user id. A request is authenticated iff an ``ory_session_*``
    cookie is present.
    """
    jar = read_cookies(user_data_dir, host_contains="mistral.ai")
    out: dict[str, str] = {}
    for name, val in jar.items():
        if (name.startswith("ory_session_")
                or name.startswith("ory_kratos_")
                or name.startswith("csrf_token_")
                or name in ("anonymousUser", "last-product-surface")):
            out[name] = val
    return out