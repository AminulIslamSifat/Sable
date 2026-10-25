#!/usr/bin/env python3
"""conformance.py — Python side of the vault conformance test.

Modes:
  primitives                  — emit hex for cross-checking against crypto.js
  seal <root> <out> <dekhex> <passphrase|''> <signkey>
  boot <vault> <mount> <dekhex> <passphrase|''>
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent / "sable-tools"
sys.path.insert(0, str(TOOLS))

import sable_crypto as sc  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305  # noqa: E402

MASTER = bytes.fromhex("a" * 64)
FILE_PATH = "engine/foo.py"
KEK = bytes.fromhex("b" * 64)
NONCE = bytes.fromhex("00" * 12)
PLAIN = b"hello sable\n"


def primitives() -> dict:
    k3 = sc.derive_file_key(MASTER, FILE_PATH, None)
    k4 = sc.derive_file_key(MASTER, FILE_PATH, KEK)
    ct = ChaCha20Poly1305(k3).encrypt(NONCE, PLAIN, None)
    obj = {"b": 1, "a": [1, 2, {"z": True, "y": None}], "c": "x"}
    return {
        "file_key_v3": k3.hex(),
        "file_key_v4": k4.hex(),
        "ct": ct.hex(),
        "canonical": json.dumps(obj, sort_keys=True, separators=(",", ":")),
    }


def do_seal(root: Path, out: Path, dek: bytes, passphrase: str | None, sign_key: Path) -> None:
    import seal as seal_mod

    seal_mod.fetch_dek = lambda bridge, token: dek
    argv = [
        "sable-seal", str(root), "-o", str(out),
        "--sign-key", str(sign_key), "--token", "fake",
    ]
    argv += ["--passphrase", passphrase] if passphrase else ["--no-passphrase"]
    old = sys.argv
    sys.argv = argv
    try:
        rc = seal_mod.main()
    finally:
        sys.argv = old
    if rc != 0:
        raise SystemExit(rc)


def do_boot(vault: Path, mount: Path, dek: bytes, passphrase: str | None) -> None:
    import boot as boot_mod

    boot_mod.fetch_dek = lambda bridge, token, vault_hash: dek
    argv = ["sable-boot", str(vault), "--mount", str(mount), "--token", "fake"]
    if passphrase:
        argv += ["--passphrase", passphrase, "--no-cache"]
    old = sys.argv
    sys.argv = argv
    try:
        rc = boot_mod.main()
    finally:
        sys.argv = old
    if rc != 0:
        raise SystemExit(rc)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "primitives"
    if mode == "primitives":
        print(json.dumps(primitives(), indent=2))
    elif mode == "seal":
        root, out, dek_hex, pw, sk = sys.argv[2:7]
        do_seal(Path(root), Path(out), bytes.fromhex(dek_hex), pw or None, Path(sk))
    elif mode == "boot":
        vault, mount, dek_hex, pw = sys.argv[2:6]
        do_boot(Path(vault), Path(mount), bytes.fromhex(dek_hex), pw or None)
    else:
        raise SystemExit(f"unknown mode: {mode}")
