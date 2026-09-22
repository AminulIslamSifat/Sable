"""sable_crypto — shared primitives for the Sable vault (v3 + v4).

Both seal.py and boot.py import this, so key derivation can never drift
between the two sides. If these disagree, nothing decrypts.

  v3 : file_key = HKDF(master, salt=None, info="sable/v3/file/"+path)
  v4 : file_key = HKDF(master, salt=KEK,   info="sable/v3/file/"+path)
       KEK = Argon2id(passphrase, vault_salt)

v4 means a file key needs BOTH secrets: the bridge's master DEK *and* the
user's passphrase. Lose either one and the vault stays shut.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# ── format ──────────────────────────────────────────────────────
MAGIC_V3 = b"SABLEV03"
MAGIC_V4 = b"SABLEV04"
MAGIC_BY_VERSION = {3: MAGIC_V3, 4: MAGIC_V4}

# Fields covered by the Ed25519 signature, per vault version.
# v4 additionally binds key_hash + salt + kdf, so downgrade games are loud.
SIGNED_FIELDS = {
    3: ("v", "created", "alg", "manifest"),
    4: ("v", "created", "alg", "key_hash", "salt", "kdf", "manifest"),
}

ALG_V3 = "chacha20poly1305-hkdf-perfile"
ALG_V4 = "chacha20poly1305-hkdf-perfile-argon2"

# ── KDF parameters ──────────────────────────────────────────────
KDF_ARGON2 = "argon2id"
KDF_PBKDF2 = "pbkdf2-sha256"

# ~0.5-1s / 256 MiB per guess on a modern laptop. Tune upward over time.
ARGON2_TIME = 3
ARGON2_MEM_KIB = 256 * 1024
ARGON2_LANES = 4
SALT_LEN = 16
KEK_LEN = 32

# Fallback if no Argon2 backend exists. Still expensive, just less memory-hard.
PBKDF2_ITERS = 600_000

KEYRING_SERVICE = "sable-vault"


def new_salt() -> bytes:
    return os.urandom(SALT_LEN)


# ── KEK derivation ──────────────────────────────────────────────
def pick_kdf() -> str:
    """Best KDF available on THIS machine. Used at seal time only."""
    return KDF_ARGON2 if _argon2_ok() else KDF_PBKDF2


def _argon2_ok() -> bool:
    try:
        from cryptography.hazmat.primitives.kdf.argon2 import Argon2id  # noqa: F401
        return True
    except Exception:
        pass
    try:
        import argon2  # noqa: F401
        return True
    except Exception:
        return False


def derive_kek(passphrase: str, salt: bytes, kdf: str) -> bytes:
    """passphrase + salt -> 32-byte KEK, using the named KDF exactly.

    boot passes the kdf recorded in the header; seal passes pick_kdf().
    Never auto-detect on the boot side — a mismatch would silently
    derive the wrong key.
    """
    pw = passphrase.encode("utf-8")
    if kdf == KDF_ARGON2:
        kek = _argon2_cryptography(pw, salt)
        if kek is not None:
            return kek
        kek = _argon2_cffi(pw, salt)
        if kek is not None:
            return kek
        raise RuntimeError(
            "vault needs argon2id but no Argon2 backend is installed "
            "(pip install argon2-cffi)"
        )
    if kdf == KDF_PBKDF2:
        return hashlib.pbkdf2_hmac("sha256", pw, salt, PBKDF2_ITERS, dklen=KEK_LEN)
    raise RuntimeError(f"unknown kdf: {kdf!r}")


def _argon2_cryptography(pw: bytes, salt: bytes) -> bytes | None:
    try:
        from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
    except Exception:
        return None
    try:
        return Argon2id(
            salt=salt,
            length=KEK_LEN,
            iterations=ARGON2_TIME,
            lanes=ARGON2_LANES,
            memory_cost=ARGON2_MEM_KIB,
        ).derive(pw)
    except Exception:
        return None


def _argon2_cffi(pw: bytes, salt: bytes) -> bytes | None:
    try:
        from argon2.low_level import Type, hash_secret_raw
    except Exception:
        return None
    try:
        return hash_secret_raw(
            secret=pw,
            salt=salt,
            time_cost=ARGON2_TIME,
            memory_cost=ARGON2_MEM_KIB,
            parallelism=ARGON2_LANES,
            hash_len=KEK_LEN,
            type=Type.ID,
        )
    except Exception:
        return None


# ── per-file key ────────────────────────────────────────────────
def derive_file_key(master: bytes, path: str, kek: bytes | None = None) -> bytes:
    """Per-file key. When kek is set it is the HKDF salt, so the key
    depends on both the bridge master DEK and the user passphrase."""
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=kek,  # None => v3 behaviour
        info=b"sable/v3/file/" + path.encode(),
    ).derive(master)


# ── OS keyring (so the user types the passphrase once) ──────────
def keyring_lookup(account: str) -> str | None:
    if shutil.which("secret-tool"):
        try:
            r = subprocess.run(
                ["secret-tool", "lookup", "service", KEYRING_SERVICE, "account", account],
                capture_output=True, timeout=10,
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.decode().strip()
        except Exception:
            pass
    if shutil.which("security"):  # macOS
        try:
            r = subprocess.run(
                ["security", "find-generic-password", "-s", KEYRING_SERVICE,
                 "-a", account, "-w"],
                capture_output=True, timeout=10,
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.decode().strip()
        except Exception:
            pass
    return None


def keyring_store(account: str, secret: str) -> bool:
    if shutil.which("secret-tool"):
        try:
            r = subprocess.run(
                ["secret-tool", "store", "--label", f"Sable vault ({account[:12]})",
                 "service", KEYRING_SERVICE, "account", account],
                input=secret.encode(), capture_output=True, timeout=10,
            )
            if r.returncode == 0:
                return True
        except Exception:
            pass
    if shutil.which("security"):  # macOS
        try:
            r = subprocess.run(
                ["security", "add-generic-password", "-U", "-s", KEYRING_SERVICE,
                 "-a", account, "-w", secret],
                capture_output=True, timeout=10,
            )
            if r.returncode == 0:
                return True
        except Exception:
            pass
    return False
