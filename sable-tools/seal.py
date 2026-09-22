#!/usr/bin/env python3
"""sable-seal — pack a Sable tree into vault.sable with PER-FILE keys.

ONE master DEK from the bridge. Each file's key is derived deterministically:
    key_i = HKDF-SHA256(master, salt=KEK, info="sable/v3/file/" + path)

v3 = no passphrase (salt=None).
v4 = KEK = Argon2id(passphrase, vault_salt) becomes the HKDF salt, so every
     file key needs BOTH the bridge DEK and the user passphrase.

Brain/, instruction/, system/ are encrypted but NOT hash-checked (they mutate).

.shipping rule: .gitignore is the authority. The only hard skips are build
cruft (.venv, __pycache__, sable-tools, test, output, browser-data-*).
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import struct
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from nacl.signing import SigningKey

import sable_crypto as sc

DEFAULT_BRIDGE = "http://127.0.0.1:8765"

# hard skips, regardless of git state (the sealer, scratch, build output)
SKIP_DIRS = {
    ".venv", ".git", "__pycache__", ".pytest_cache", ".sable_backups",
    ".editor_tools_backups", "node_modules", ".mypy_cache", ".ruff_cache",
    ".idea", ".vscode", "fastembed_cache", "models",
    "sable-tools", "test", "tests", "output", "dist",
}
SKIP_DIR_PREFIXES = ("browser-data-", "browser-scraper-data", "browser-automation-data")
SKIP_FILES = {".sable_server.pid", "vault.sable", ".DS_Store"}

# excluded from the no-change (hash) check — they mutate at runtime
NO_VERIFY_PREFIXES = ("Brain/", "instruction/", "system/")


def git_visible(root: Path) -> set[str] | None:
    """Paths git does NOT ignore (tracked + untracked-unignored).

    This is git's own answer to "what is not ignored" — nested .gitignore
    files, negation patterns and core.excludesFile all count. Returns None
    when the tree isn't a git repo.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "--others",
             "--exclude-standard", "-z"],
            capture_output=True, check=True, timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return {p for p in out.decode("utf-8", "replace").split("\0") if p}


def should_skip(rel: Path, visible: set[str] | None) -> bool:
    path = str(rel)
    for part in rel.parts:
        if part in SKIP_DIRS:
            return True
        if any(part.startswith(p) for p in SKIP_DIR_PREFIXES):
            return True
    if rel.name in SKIP_FILES:
        return True

    # .gitignore decides everything else — no exceptions
    if visible is not None and path not in visible:
        return True
    return False


def collect(root: Path) -> list[Path]:
    visible = git_visible(root)
    if visible is None:
        print("[seal] WARN: not a git repo — .gitignore cannot be honoured")
    rels = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if should_skip(rel, visible):
            continue
        rels.append(rel)
    return sorted(rels)


def needs_verify(path: str) -> bool:
    return not any(path.startswith(p) for p in NO_VERIFY_PREFIXES)


def load_signer(path: Path) -> SigningKey:
    if path.exists():
        return SigningKey(path.read_bytes())
    path.parent.mkdir(parents=True, exist_ok=True)
    sk = SigningKey.generate()
    path.write_bytes(sk.encode())
    path.chmod(0o600)
    print(f"[seal] generated signing key -> {path}")
    return sk


def fetch_dek(bridge: str, token: str) -> bytes:
    r = httpx.get(f"{bridge}/v1/key",
                  headers={"Authorization": f"Bearer {token}"}, timeout=10)
    r.raise_for_status()
    return bytes.fromhex(r.json()["dek"])


def resolve_passphrase(args) -> str | None:
    """seal-side passphrase resolution. --no-passphrase wins."""
    if args.no_passphrase:
        return None
    if args.passphrase is not None:
        return args.passphrase
    if args.passphrase_file:
        return Path(args.passphrase_file).read_text(encoding="utf-8").rstrip("\n")
    env = os.environ.get("SABLE_PASSPHRASE")
    if env:
        return env
    if not sys.stdin.isatty():
        return None
    p1 = getpass.getpass("[seal] vault passphrase (blank = none): ")
    if not p1:
        return None
    p2 = getpass.getpass("[seal] confirm passphrase: ")
    if p1 != p2:
        raise SystemExit("FATAL: passphrases did not match")
    return p1


def main() -> int:
    ap = argparse.ArgumentParser(prog="sable-seal")
    ap.add_argument("root", nargs="?", default=".", type=Path)
    ap.add_argument("-o", "--out", default="vault.sable", type=Path)
    ap.add_argument("--bridge", default=os.environ.get("SABLE_BRIDGE", DEFAULT_BRIDGE))
    ap.add_argument("--token", default=os.environ.get("SABLE_BRIDGE_TOKEN", ""))
    ap.add_argument("--sign-key", default=Path.home() / ".config" / "sable" / "dev.ed25519",
                    type=Path)
    ap.add_argument("--passphrase", default=None, help="inline passphrase (avoid; leaks to ps)")
    ap.add_argument("--passphrase-file", default=None)
    ap.add_argument("--no-passphrase", action="store_true",
                    help="seal v3 (bridge DEK only)")
    args = ap.parse_args()

    if not args.token:
        raise SystemExit("FATAL: no bridge token.\n  set SABLE_BRIDGE_TOKEN or pass --token.")

    root = args.root.resolve()
    if not root.is_dir():
        raise SystemExit(f"not a directory: {root}")

    rels = collect(root)
    print(f"[seal] {len(rels)} files from {root}")

    # ── passphrase -> KEK ───────────────────────────────────────
    passphrase = resolve_passphrase(args)
    if passphrase:
        salt = sc.new_salt()
        kdf = sc.pick_kdf()
        print(f"[seal] deriving KEK ({kdf}, {sc.ARGON2_MEM_KIB//1024} MiB, "
              f"{sc.ARGON2_TIME} iters)...")
        kek = sc.derive_kek(passphrase, salt, kdf)
        version = 4
        alg = sc.ALG_V4
    else:
        salt, kdf, kek = None, None, None
        version = 3
        alg = sc.ALG_V3
        print("[seal] no passphrase — v3 (bridge DEK only)")

    print(f"[seal] fetching master DEK from {args.bridge}")
    master = fetch_dek(args.bridge, args.token)
    master_hash = hashlib.sha256(master).hexdigest()
    print(f"[seal] master key: {master_hash[:16]}...")

    files_meta, nonces, blobs = [], [], []
    n_verified = 0
    for rel in rels:
        path = str(rel)
        data = (root / rel).read_bytes()
        verify = needs_verify(path)
        digest = hashlib.sha256(data).hexdigest() if verify else ""
        if verify:
            n_verified += 1
        key = sc.derive_file_key(master, path, kek)
        nonce = os.urandom(12)
        ct = ChaCha20Poly1305(key).encrypt(nonce, data, None)
        files_meta.append({"path": path, "size": len(data), "sha256": digest, "verify": verify})
        nonces.append(nonce.hex())
        blobs.append(ct)

    joined = "".join(f"{f['path']}:{f['sha256']}" for f in files_meta).encode()
    manifest = {
        "files": files_meta,
        "root": hashlib.sha256(joined).hexdigest(),
        "count": len(files_meta),
        "verified": n_verified,
    }
    print(f"[seal] manifest root: {manifest['root'][:16]}...  "
          f"({n_verified} hash-checked, {len(rels)-n_verified} excluded)")

    sk = load_signer(args.sign_key)
    header = {
        "v": version,
        "created": datetime.now(timezone.utc).isoformat(),
        "alg": alg,
        "key_hash": master_hash,
        "salt": salt.hex() if salt else None,
        "kdf": kdf,
        "nonces": nonces,
        "manifest": manifest,
    }
    # signature covers the fields defined for this version
    sign_body = {k: header[k] for k in sc.SIGNED_FIELDS[version]}
    body = json.dumps(sign_body, sort_keys=True, separators=(",", ":")).encode()
    header["sig"] = sk.sign(body).signature.hex()
    header["signer"] = sk.verify_key.encode().hex()

    header_json = json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("wb") as f:
        f.write(sc.MAGIC_BY_VERSION[version])
        f.write(struct.pack(">I", len(header_json)))
        f.write(header_json)
        for ct in blobs:
            f.write(ct)

    size = args.out.stat().st_size
    print(f"[seal] wrote {args.out} ({size/1024/1024:.2f} MiB)  v{version}")
    print(f"[seal] signer pubkey: {header['signer']}")
    if passphrase:
        print("[seal] tip: sable-boot will cache the passphrase in your OS keyring")
    return 0


if __name__ == "__main__":
    sys.exit(main())
