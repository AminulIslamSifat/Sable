#!/usr/bin/env python3
"""vault-encrypt — turn a source tree into per-file .enc blobs.

Used by the import-hook path (C2). Plaintext is deleted after encryption;
--keep keeps it around during dev iterations.

    vault-encrypt core_vault/ --dek-hex <64-hex>

Each `path/foo.py` becomes `path/foo.py.enc`, where the blob is
`nonce(12) || ChaCha20-Poly1305(plaintext)`.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

SKIP_DIRS = {"__pycache__", ".venv", ".git", ".pytest_cache"}


def walk_py(root: Path) -> list[Path]:
    out: list[Path] = []
    for p in root.rglob("*.py"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        out.append(p)
    return sorted(out)


def encrypt_one(path: Path, dek: bytes) -> Path:
    blob = path.read_bytes()
    nonce = os.urandom(12)
    ct = ChaCha20Poly1305(dek).encrypt(nonce, blob, None)
    out = path.with_suffix(path.suffix + ".enc")
    out.write_bytes(nonce + ct)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(prog="vault-encrypt")
    ap.add_argument("root", type=Path)
    ap.add_argument("--dek-hex", required=True, help="32-byte DEK as 64 hex chars")
    ap.add_argument("--keep", action="store_true", help="keep plaintext .py alongside .enc")
    args = ap.parse_args()

    dek = bytes.fromhex(args.dek_hex)
    if len(dek) != 32:
        raise SystemExit("DEK must be 32 bytes (64 hex chars)")

    files = walk_py(args.root)
    print(f"[encrypt] {len(files)} .py files under {args.root}")
    for f in files:
        out = encrypt_one(f, dek)
        if not args.keep:
            f.unlink()
        print(f"  {f.relative_to(args.root)} → {out.name}")
    print(f"[encrypt] done ({'kept plaintext' if args.keep else 'plaintext removed'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
