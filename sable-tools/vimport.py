"""vimport — decrypt-on-import loader for Sable's vault.

Plaintext never touches disk. A module lives as `name.py.enc`
(nonce + ChaCha20-Poly1305 ciphertext). Import reads the blob,
decrypts in RAM, compiles, executes — the source string is then GC'd.

Usage:
    import vimport
    from pathlib import Path
    vimport.install(Path("vault/"), dek_bytes)
    import engine  # pulls from vault/engine.py.enc

Safety notes:
  - Run with -B / PYTHONDONTWRITEBYTECODE=1 — no __pycache__ leak.
  - Currently handles top-level modules only (no packages yet).
  - The DEK stays in RAM, bound to the finder instance.
"""
from __future__ import annotations

import importlib.abc
import importlib.util
import sys
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305


class VaultLoader(importlib.abc.Loader):
    def __init__(self, name: str, blob_path: Path, dek: bytes) -> None:
        self.name = name
        self.blob_path = blob_path
        self.dek = dek

    def create_module(self, spec):  # noqa: ANN001 — importlib signature
        return None

    def exec_module(self, module) -> None:  # noqa: ANN001
        raw = self.blob_path.read_bytes()
        if len(raw) < 12:
            raise ImportError(f"vault blob too small: {self.blob_path}")
        nonce, ct = raw[:12], raw[12:]
        src = ChaCha20Poly1305(self.dek).decrypt(nonce, ct, None).decode()
        code = compile(src, f"<vault:{self.name}>", "exec")
        exec(code, module.__dict__)
        del src  # best-effort — no lingering reference


class VaultFinder(importlib.abc.MetaPathFinder):
    def __init__(self, vault_dir: Path, dek: bytes, prefix: tuple[str, ...] = ()) -> None:
        self.vault_dir = vault_dir
        self.dek = dek
        self.prefix = prefix

    def find_spec(self, fullname: str, path, target=None):  # noqa: ANN001
        if "." in fullname:
            return None
        if self.prefix and not fullname.startswith(self.prefix):
            return None
        blob = self.vault_dir / f"{fullname}.py.enc"
        if not blob.exists():
            return None
        loader = VaultLoader(fullname, blob, self.dek)
        return importlib.util.spec_from_loader(fullname, loader)


def install(vault_dir: Path, dek: bytes, prefix: tuple[str, ...] = ()) -> None:
    """Register the vault finder at the front of sys.meta_path."""
    sys.meta_path.insert(0, VaultFinder(vault_dir, dek, prefix))
