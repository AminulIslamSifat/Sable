#!/usr/bin/env python3
"""build_packages.py — build Sable release artifacts.

The shipping product is NOT a source tree. It is:

    sable-starter.exe       (Blob A — Go trust root, signer pinned)
    vault.sable             (per-file-encrypted payload, signed)
    .venv/                  (python env, so no pip install on the user's box)

Everything else stays on the build side. This script orchestrates the real
ship pipeline:

    1. build the Go starter for each platform (ldflags-pinned signer)
    2. seal the tree into a vault (sable-tools/seal.py)
    3. bundle starter + vault + .venv into a per-platform zip

Runtime tree identical across platforms today; PLATFORM_FILTERS is the hook
for when it isn't (platform-only launchers, bundled binaries, etc.).

Usage:
    python3 tools/build_packages.py                    # all platforms
    python3 tools/build_packages.py --platform linux
    python3 tools/build_packages.py --no-seal          # skip re-sealing
    python3 tools/build_packages.py --signer <64-hex>  # override pinned key
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STARTER_DIR = REPO / "sable-tools" / "starter"
SEAL_SCRIPT = REPO / "sable-tools" / "seal.py"
DEFAULT_SIGN_KEY = Path.home() / ".config" / "sable" / "dev.ed25519"
DEFAULT_VENV = REPO / ".venv"
DIST = REPO / "dist"

PLATFORMS = {
    "linux":   {"goos": "linux",   "goarch": "amd64", "exe": "sable-starter"},
    "windows": {"goos": "windows", "goarch": "amd64", "exe": "sable-starter.exe"},
    "macos":   {"goos": "darwin",  "goarch": "arm64", "exe": "sable-starter"},
}

# Per-platform divergence for the payload tree. Currently: launcher filtering.
# Kept even though the shipping artifact is starter+vault+.venv, because the
# .venv copy may pick up platform-specific extras as the project grows.
PLATFORM_FILTERS: dict[str, set[str]] = {
    "linux":   {"start.bat", "start.ps1", "start_shortcut.ps1", "sable-launch.cmd"},
    "windows": {"sable-launch.sh"},
    "macos":   {"start.bat", "start.ps1", "start_shortcut.ps1", "sable-launch.cmd"},
}


def get_version() -> str:
    with open(REPO / "pyproject.toml", "rb") as f:
        return tomllib.load(f)["project"]["version"]


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    return subprocess.run(cmd, check=True, **kw)


def resolve_signer_hex(signer_arg: str | None) -> str:
    """Determine the pubkey to pin. arg > $SABLE_TRUSTED_SIGNER > derive from key file."""
    if signer_arg:
        return signer_arg.strip()
    env = os.environ.get("SABLE_TRUSTED_SIGNER", "").strip()
    if env:
        return env
    if DEFAULT_SIGN_KEY.exists():
        # derive pubkey from the Ed25519 private key file
        try:
            from nacl.signing import SigningKey
        except ImportError:
            raise SystemExit(
                "need pynacl to derive pubkey from key file, or pass --signer <64-hex>"
            )
        sk = SigningKey(DEFAULT_SIGN_KEY.read_bytes())
        return sk.verify_key.encode().hex()
    raise SystemExit(
        "no signer available:\n"
        "  pass --signer <64-hex>, set SABLE_TRUSTED_SIGNER, or create "
        f"{DEFAULT_SIGN_KEY}"
    )


def build_starter(platform: str, signer_hex: str, out_dir: Path) -> Path:
    """Compile the Go trust root with the signer pinned via ldflags."""
    spec = PLATFORMS[platform]
    out_path = out_dir / spec["exe"]
    env = {
        **os.environ,
        "GOOS": spec["goos"],
        "GOARCH": spec["goarch"],
        "CGO_ENABLED": "0",
    }
    run([
        "go", "build",
        "-trimpath",
        "-ldflags", f"-s -w -X main.trustedSignerHex={signer_hex}",
        "-o", str(out_path),
        ".",
    ], cwd=STARTER_DIR, env=env)
    return out_path


def seal_vault(out_path: Path, sign_key: Path, token: str,
               passphrase: str | None, no_passphrase: bool) -> None:
    """Seal the repo tree into a signed vault using sable-tools/seal.py."""
    cmd = [
        sys.executable, str(SEAL_SCRIPT), str(REPO),
        "-o", str(out_path),
        "--sign-key", str(sign_key),
    ]
    if no_passphrase:
        cmd.append("--no-passphrase")
    elif passphrase:
        cmd += ["--passphrase", passphrase]
    else:
        cmd.append("--no-passphrase")  # non-interactive default
    env = {**os.environ, "SABLE_BRIDGE_TOKEN": token}
    run(cmd, env=env)


def zip_artifact(
    platform: str,
    out_zip: Path,
    starter_bin: Path,
    vault: Path,
    venv_dir: Path | None,
) -> tuple[int, int]:
    """Bundle starter + vault + .venv into a zip. Returns (files, raw_bytes)."""
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    drop = PLATFORM_FILTERS.get(platform, set())
    written = 0
    raw = 0

    def _add(zf: zipfile.ZipFile, src: Path, arcname: str) -> int:
        zf.write(src, arcname)
        try:
            return src.stat().st_size
        except OSError:
            return 0

    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        # 1. starter binary
        raw += _add(zf, starter_bin, starter_bin.name)
        written += 1

        # 2. vault
        raw += _add(zf, vault, vault.name)
        written += 1

        # 3. .venv (if present)
        if venv_dir and venv_dir.is_dir():
            for root, dirs, files in os.walk(venv_dir):
                root_p = Path(root)
                # prune __pycache__ and .pyc cruft
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for fn in files:
                    if fn.endswith((".pyc", ".pyo")):
                        continue
                    if fn in drop:
                        continue
                    fp = root_p / fn
                    rel = fp.relative_to(venv_dir.parent)
                    raw += _add(zf, fp, str(rel))
                    written += 1

    return written, raw


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default=None)
    ap.add_argument("--platform", choices=[*PLATFORMS, "all"], default="all")
    ap.add_argument("--out", default=str(DIST))
    ap.add_argument("--signer", default=None, help="64-hex Ed25519 pubkey to pin")
    ap.add_argument("--sign-key", default=str(DEFAULT_SIGN_KEY), type=Path)
    ap.add_argument("--token", default=os.environ.get("SABLE_BRIDGE_TOKEN", ""),
                    help="bridge bearer token (needed for sealing)")
    ap.add_argument("--passphrase", default=None, help="v4 passphrase (optional)")
    ap.add_argument("--no-passphrase", action="store_true",
                    help="seal v3 (default when no passphrase given)")
    ap.add_argument("--no-seal", action="store_true",
                    help="reuse an existing vault at <out>/vault.sable")
    ap.add_argument("--venv", default=str(DEFAULT_VENV), type=Path)
    ap.add_argument("--no-venv", action="store_true",
                    help="omit the .venv from the bundle")
    args = ap.parse_args()

    version = args.version or get_version()
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    signer_hex = resolve_signer_hex(args.signer)
    print(f"📦 Sable v{version} → {out_dir}")
    print(f"   pinned signer: {signer_hex[:16]}…")

    platforms = list(PLATFORMS) if args.platform == "all" else [args.platform]

    # seal once — the vault is platform-agnostic (runtime branches on GOOS)
    vault_path = out_dir / "vault.sable"
    if args.no_seal and vault_path.exists():
        print(f"\n[seal] reusing {vault_path}")
    else:
        if not args.token:
            raise SystemExit("\nFATAL: sealing needs --token or $SABLE_BRIDGE_TOKEN")
        print(f"\n[seal] packing tree → {vault_path}")
        seal_vault(
            vault_path, args.sign_key, args.token,
            args.passphrase, args.no_passphrase,
        )

    venv_dir = None if args.no_venv else args.venv
    if venv_dir and not venv_dir.is_dir():
        print(f"   WARN: venv {venv_dir} not found — bundle will omit it")
        venv_dir = None

    print()
    for p in platforms:
        spec = PLATFORMS[p]
        starter_bin = out_dir / f"{p}-build" / spec["exe"]
        starter_bin.parent.mkdir(parents=True, exist_ok=True)

        print(f"[{p}] building starter…")
        build_starter(p, signer_hex, starter_bin.parent)

        zip_path = out_dir / f"sable-{version}-{p}.zip"
        print(f"[{p}] zipping → {zip_path.name}")
        written, raw = zip_artifact(p, zip_path, starter_bin, vault_path, venv_dir)
        size_mb = zip_path.stat().st_size / (1024 * 1024)
        print(f"[{p}] ✓ {written} files, {size_mb:.1f} MB compressed "
              f"({raw / 1024 / 1024:.1f} MB raw)\n")

    print("✅ done")
    for p in platforms:
        print(f"   {out_dir / f'sable-{version}-{p}.zip'}")


if __name__ == "__main__":
    main()
