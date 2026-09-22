#!/usr/bin/env python3
"""sable-boot — decrypt vault.sable (per-file keys) into tmpfs + verify each.

Supports v3 (bridge DEK only) and v4 (bridge DEK + user passphrase KEK).
Passphrase resolution order for v4:
  1. --passphrase
  2. $SABLE_PASSPHRASE
  3. OS keyring (cached from a previous boot)
  4. interactive prompt (then cached, unless --no-cache)
Plaintext lands in tmpfs only. Run with -B so no __pycache__ leaks.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

import sable_crypto as sc

DEFAULT_BRIDGE = "http://127.0.0.1:8765"


def read_vault(path: Path):
    data = path.read_bytes()
    if len(data) < 12:
        raise SystemExit("FATAL: file too small to be a vault")
    magic = data[:8]
    if magic not in (sc.MAGIC_V3, sc.MAGIC_V4):
        raise SystemExit(f"not a sable vault (magic={magic!r})")
    hlen = struct.unpack(">I", data[8:12])[0]
    return json.loads(data[12:12+hlen]), data[12+hlen:]


def verify(header: dict) -> None:
    version = header["v"]
    fields = sc.SIGNED_FIELDS.get(version)
    if fields is None:
        raise SystemExit(f"FATAL: unsupported vault version {version}")
    sig = bytes.fromhex(header["sig"])
    signer = bytes.fromhex(header["signer"])
    try:
        body = {k: header[k] for k in fields}
    except KeyError as e:
        raise SystemExit(f"FATAL: vault header missing signed field {e}")
    body_bytes = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    try:
        VerifyKey(signer).verify(body_bytes, sig)
    except BadSignatureError:
        raise SystemExit("FATAL: manifest signature invalid — vault tampered with")
    print("[boot] ok  manifest signature valid")


def fetch_cloud_token(cloud: str, vault_hash: str, token: str) -> str:
    r = httpx.post(f"{cloud.rstrip('/')}/v1/token",
                   json={"vault_hash": vault_hash},
                   headers={"Authorization": f"Bearer {token}"}, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


def fetch_dek(bridge: str, token: str, vault_hash: str) -> bytes:
    r = httpx.get(f"{bridge}/v1/key", params={"vault_hash": vault_hash},
                  headers={"Authorization": f"Bearer {token}"}, timeout=10)
    r.raise_for_status()
    return bytes.fromhex(r.json()["dek"])


def resolve_passphrase(header: dict, args, account: str) -> str | None:
    """v4 passphrase: arg -> env -> keyring -> prompt (then cache)."""
    if args.passphrase is not None:
        return args.passphrase
    env = os.environ.get("SABLE_PASSPHRASE")
    if env:
        return env

    cached = sc.keyring_lookup(account)
    if cached:
        print("[boot] ok  passphrase from OS keyring")
        return cached

    if not sys.stdin.isatty():
        raise SystemExit(
            "FATAL: vault needs a passphrase but stdin is not a TTY.\n"
            "  pass --passphrase, or set SABLE_PASSPHRASE."
        )
    pw = getpass.getpass("[boot] vault passphrase: ")
    if not pw:
        raise SystemExit("FATAL: empty passphrase")
    if not args.no_cache:
        if sc.keyring_store(account, pw):
            print("[boot] ok  passphrase cached in OS keyring")
        else:
            print("[boot] note: no OS keyring found — you'll be asked again next boot")
    return pw


def pick_mount(hint):
    if hint:
        return hint
    if Path("/dev/shm").is_dir():
        return Path("/dev/shm/sable-vault")
    return Path(tempfile.mkdtemp(prefix="sable-vault-"))


def main() -> int:
    ap = argparse.ArgumentParser(prog="sable-boot")
    ap.add_argument("vault", type=Path)
    ap.add_argument("--bridge", default=os.environ.get("SABLE_BRIDGE", DEFAULT_BRIDGE))
    ap.add_argument("--token", default=os.environ.get("SABLE_BRIDGE_TOKEN", ""))
    ap.add_argument("--cloud", default=os.environ.get("SABLE_CLOUD", ""))
    ap.add_argument("--mount", type=Path)
    ap.add_argument("--venv", type=Path)
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--passphrase", default=None)
    ap.add_argument("--no-cache", action="store_true",
                    help="don't store the passphrase in the OS keyring")
    args = ap.parse_args()

    print(f"[boot] vault: {args.vault}")
    header, body = read_vault(args.vault)
    version = header["v"]
    man = header["manifest"]
    print(f"[boot]   version: v{version}  ({header.get('alg')})")
    print(f"[boot]   sealed:  {header['created']}")
    print(f"[boot]   files:   {man['count']} ({man['verified']} hash-checked)")

    verify(header)

    token = args.token
    if not token and args.cloud:
        if not args.token:
            raise SystemExit("FATAL: --cloud needs --token (or SABLE_BRIDGE_TOKEN) for auth")
        token = fetch_cloud_token(args.cloud, header["key_hash"], args.token)
        print("[boot] ok  cloud token acquired")
    if not token:
        raise SystemExit("FATAL: no token. pass --cloud or set SABLE_BRIDGE_TOKEN.")

    master = fetch_dek(args.bridge, token, header["key_hash"])
    if hashlib.sha256(master).hexdigest() != header["key_hash"]:
        raise SystemExit("FATAL: master key mismatch")
    print("[boot] ok  master key matches")

    # ── v4: passphrase -> KEK (the second lock) ─────────────────
    kek = None
    if version == 4:
        salt_hex, kdf = header.get("salt"), header.get("kdf")
        if not salt_hex or not kdf:
            raise SystemExit("FATAL: v4 vault missing salt/kdf in header")
        account = header["key_hash"]
        passphrase = resolve_passphrase(header, args, account)
        kek = sc.derive_kek(passphrase, bytes.fromhex(salt_hex), kdf)
        # NOT "accepted" yet — a wrong passphrase only shows up at decrypt time.
        print("[boot] ok  KEK derived — verifying on first file...")

    mount = pick_mount(args.mount)
    if mount.exists():
        shutil.rmtree(mount)
    mount.mkdir(parents=True)

    nonces = header["nonces"]
    off = 0
    n_verified = 0
    for i, fm in enumerate(man["files"]):
        ct_len = fm["size"] + 16  # + poly1305 tag
        ct = body[off:off+ct_len]
        off += ct_len
        key = sc.derive_file_key(master, fm["path"], kek)
        nonce = bytes.fromhex(nonces[i])
        try:
            plain = ChaCha20Poly1305(key).decrypt(nonce, ct, None)
        except Exception:
            if version == 4 and i == 0:
                raise SystemExit(
                    "FATAL: wrong passphrase (or tampered vault) — "
                    f"first file {fm['path']} failed to decrypt"
                )
            raise SystemExit(f"FATAL: decrypt failed for {fm['path']} — tampered or corrupt")
        if fm["verify"]:
            got = hashlib.sha256(plain).hexdigest()
            if got != fm["sha256"]:
                raise SystemExit(
                    f"FATAL: hash mismatch for {fm['path']}\n"
                    f"  expected {fm['sha256'][:16]}...\n"
                    f"  got      {got[:16]}..."
                )
            n_verified += 1
        dest = mount / fm["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(plain)

    print(f"[boot] ok  {len(man['files'])} files decrypted, {n_verified} hash-verified")
    print(f"[boot] ok  extracted -> {mount}")

    if not args.run:
        return 0
    server_py = mount / "server.py"
    if not server_py.exists():
        raise SystemExit(f"no server.py at {server_py}")
    if args.venv:
        cands = [args.venv / "bin" / "python", args.venv / "Scripts" / "python.exe"]
        python_bin = next((c for c in cands if c.exists()), None)
        if not python_bin:
            raise SystemExit(f"no python in {args.venv}")
    else:
        python_bin = Path(sys.executable)
    print(f"[boot] spawn: {python_bin} -B {server_py}")
    env = {**os.environ, "SABLE_BOOT_MOUNT": str(mount), "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.call([str(python_bin), "-B", str(server_py)], cwd=str(mount), env=env)


if __name__ == "__main__":
    sys.exit(main())
