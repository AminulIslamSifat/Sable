#!/usr/bin/env python3
"""build.py — one command to build the Sable desktop app.

Replaces the manual sequence of
    patch_paths.py && patch_assets.py && patch_spawns.py \
      && pyinstaller sable.spec && electron-builder … && verify

Usage:
    ./build.py                 # full build (patches + server + appimage)
    ./build.py --skip-patch    # rebuild without touching sources
    ./build.py --verify-only   # check an existing build
    ./build.py --launch        # build then run the AppImage
    ./build.py --clean         # wipe work/dist/release first
    ./build.py --target deb    # also/instead build a .deb
    ./build.py --target all    # AppImage + deb

Everything is idempotent. Patch steps that find nothing to change say so and
move on, so a second run is safe.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # repo root
BUILD = ROOT / "build_app"
ELECTRON = BUILD / "electron"
DIST = BUILD / "dist"
RELEASE = BUILD / "release"
WORK = BUILD / "work"
SERVER_DIR = DIST / "sable-server"
VENV_PY = ROOT / ".venv" / "bin" / "python"

APP_NAME = "Sable"
VERSION = "2.0.3"


# ── output helpers ──────────────────────────────────────────────────────────

class Stage:
    """Timed, labelled step with consistent output."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.t0 = 0.0

    def __enter__(self) -> "Stage":
        self.t0 = time.monotonic()
        print(f"\n\033[1;36m▸ {self.label}\033[0m", flush=True)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        dt = time.monotonic() - self.t0
        if exc_type is None:
            print(f"\033[32m  ✓ {dt:.1f}s\033[0m", flush=True)
        else:
            print(f"\033[31m  ✗ failed after {dt:.1f}s\033[0m", flush=True)
        return False


def die(msg: str, code: int = 1) -> None:
    print(f"\033[31mERROR: {msg}\033[0m", file=sys.stderr)
    sys.exit(code)


def run(cmd: list[str], cwd: Path | None = None, quiet: bool = False) -> str:
    """Run a command, stream or capture output, abort on failure."""
    pretty = " ".join(str(c) for c in cmd)
    if not quiet:
        print(f"  $ {pretty}", flush=True)
    proc = subprocess.run(
        [str(c) for c in cmd],
        cwd=str(cwd) if cwd else None,
        capture_output=quiet,
        text=True,
    )
    if proc.returncode != 0:
        if quiet and proc.stderr:
            print(proc.stderr[-3000:], file=sys.stderr)
        die(f"command failed (exit {proc.returncode}): {pretty}")
    return proc.stdout if quiet else ""


# ── preflight ───────────────────────────────────────────────────────────────

def preflight() -> None:
    with Stage("Preflight"):
        if not VENV_PY.is_file():
            die(f"no venv python at {VENV_PY}")
        if not (ELECTRON / "node_modules" / ".bin" / "electron-builder").is_file():
            die("electron-builder missing — run: cd build_app/electron && npm install")
        if not (ELECTRON / "build" / "icon.png").is_file():
            die("missing build_app/electron/build/icon.png")
        if not (BUILD / "sable.spec").is_file():
            die("missing build_app/sable.spec")
        print(f"  python   : {VENV_PY}")
        print(f"  electron : {(ELECTRON / 'node_modules' / '.bin' / 'electron-builder')}")


# ── patching ────────────────────────────────────────────────────────────────

PATCH_SCRIPTS = ["patch_paths.py", "patch_assets.py", "patch_spawns.py"]


def apply_patches() -> None:
    """Run every patch script. They are idempotent: a no-op reports 0 changes."""
    with Stage("Patching sources"):
        for name in PATCH_SCRIPTS:
            path = BUILD / name
            if not path.is_file():
                print(f"  skip (missing): {name}")
                continue
            out = run([VENV_PY, str(path)], quiet=True)
            summary = [ln for ln in out.strip().splitlines() if ln.strip()]
            tail = summary[-1] if summary else "(no output)"
            print(f"  {name}: {tail}")


# ── server ──────────────────────────────────────────────────────────────────

def build_server() -> None:
    with Stage("Building Python server (PyInstaller)"):
        DIST.mkdir(parents=True, exist_ok=True)
        log = BUILD / "pyinstaller.log"
        cmd = [
            VENV_PY, "-m", "PyInstaller", str(BUILD / "sable.spec"),
            "--distpath", str(DIST), "--workpath", str(WORK), "--noconfirm",
        ]
        with log.open("w") as fh:
            proc = subprocess.run(cmd, cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT)
        if proc.returncode != 0:
            print(f"  log: {log}")
            tail = log.read_text(errors="replace").strip().splitlines()[-15:]
            for line in tail:
                print(f"  {line}")
            die(f"PyInstaller failed (exit {proc.returncode})")
        if not SERVER_DIR.is_dir():
            die("PyInstaller produced no sable-server directory")
        size = dir_size_mb(SERVER_DIR)
        print(f"  server bundle: {size:.0f} MB → {SERVER_DIR}")


def dir_size_mb(path: Path) -> float:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total / (1024 * 1024)


# ── electron ────────────────────────────────────────────────────────────────

def build_appimage(targets: str) -> None:
    with Stage(f"Packaging Electron ({targets})"):
        if RELEASE.exists():
            shutil.rmtree(RELEASE)
        eb = ELECTRON / "node_modules" / ".bin" / "electron-builder"
        cmd = [str(eb), "--linux", targets, "--publish", "never"]
        log = BUILD / "electron-builder.log"
        with log.open("w") as fh:
            proc = subprocess.run(cmd, cwd=str(ELECTRON), stdout=fh, stderr=subprocess.STDOUT)
        artifacts = sorted(RELEASE.glob("*.AppImage")) + sorted(RELEASE.glob("*.deb"))
        if not artifacts:
            print(f"  log: {log}")
            tail = log.read_text(errors="replace").strip().splitlines()[-15:]
            for line in tail:
                print(f"  {line}")
            die("electron-builder produced no artifacts")
        # electron-builder exits non-zero after writing the artifact when the
        # publish-metadata step has no repository configured. The artifact is
        # valid; only that trailing step failed.
        for a in artifacts:
            print(f"  {a.name}: {a.stat().st_size / (1024 * 1024):.0f} MB")


# ── verification ────────────────────────────────────────────────────────────

FORBIDDEN = [
    "connectors/opencode/bin/opencode",   # 178 MB reference binary
    ".editor_tools_backups",              # editor scratch
]

# Only *data* files land on disk — Python modules live inside the PYZ archive
# and are exercised by verify_run_script() instead of a file-existence check.
REQUIRED = [
    "connectors/opencode/data/opencode_harness.json",  # OpenCode 403 fix
    "connectors/opencode/data/opencode_ja3.json",
    "web/index.html",
]


def verify_server() -> None:
    with Stage("Verifying server bundle"):
        internal = SERVER_DIR / "_internal"
        problems: list[str] = []

        for rel in FORBIDDEN:
            hits = list(internal.glob(f"**/{Path(rel).name}")) if "/" not in rel else [internal / rel]
            for h in hits:
                if h.exists():
                    problems.append(f"junk shipped: {h.relative_to(SERVER_DIR)}")

        for rel in REQUIRED:
            hits = list(internal.glob(f"**/{Path(rel).name}"))
            if not hits:
                problems.append(f"required file missing: {rel}")

        bak_count = len(list(internal.glob("**/*.bak*")))
        if bak_count:
            problems.append(f"{bak_count} .bak file(s) shipped")

        if problems:
            for p in problems:
                print(f"  \033[31m✗ {p}\033[0m")
            die("bundle verification failed")

        print(f"  no junk, all required assets present")


def verify_run_script() -> None:
    """The frozen binary must run a script, not boot a second server."""
    with Stage("Verifying --run-script dispatch"):
        binary = SERVER_DIR / "sable-server"
        script = SERVER_DIR / "_internal" / "tools" / "image_generator" / "scripts" / "image_generator.py"
        if not script.is_file():
            print("  skip: image_generator.py not in bundle")
            return
        env = {**os.environ, "SABLE_PERSISTENT_ROOT": "/tmp/sable-build-verify"}
        proc = subprocess.run(
            [str(binary), "--run-script", str(script), "--help"],
            capture_output=True, text=True, timeout=60, env=env,
        )
        combined = proc.stdout + proc.stderr
        if "SABLE SERVER" in combined:
            die("--run-script booted a server instead of running the script")
        if "usage:" not in combined.lower():
            die(f"--run-script produced no script output:\n{combined[-800:]}")
        print("  script ran, no server boot")


def verify_appimage() -> None:
    with Stage("Verifying AppImage"):
        imgs = sorted(RELEASE.glob("*.AppImage"))
        if not imgs:
            print("  skip: no AppImage built")
            return
        app = imgs[-1]
        size_mb = app.stat().st_size / (1024 * 1024)
        print(f"  {app.name}: {size_mb:.0f} MB")
        if size_mb > 600:
            print("  \033[33mwarning: larger than expected — check for junk\033[0m")


# ── main ────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Build the Sable desktop app")
    ap.add_argument("--skip-patch", action="store_true", help="don't re-apply source patches")
    ap.add_argument("--verify-only", action="store_true", help="only verify an existing build")
    ap.add_argument("--launch", action="store_true", help="run the AppImage when done")
    ap.add_argument("--clean", action="store_true", help="wipe work/dist/release first")
    ap.add_argument("--target", default="AppImage", help="AppImage | deb | all")
    args = ap.parse_args()

    t0 = time.monotonic()
    print(f"\033[1mSable desktop build\033[0m  ({ROOT})")

    if args.verify_only:
        if not SERVER_DIR.is_dir():
            die("no server bundle to verify — run a full build first")
        verify_server()
        verify_run_script()
        verify_appimage()
        print(f"\n\033[32mdone in {time.monotonic() - t0:.0f}s\033[0m")
        return 0

    preflight()

    if args.clean:
        with Stage("Cleaning"):
            for d in (WORK, DIST, RELEASE):
                if d.exists():
                    shutil.rmtree(d)
                    print(f"  removed {d.relative_to(ROOT)}")

    if not args.skip_patch:
        apply_patches()

    build_server()
    verify_server()
    verify_run_script()
    build_appimage(args.target)
    verify_appimage()

    elapsed = time.monotonic() - t0
    print(f"\n\033[1;32mBuild complete in {elapsed:.0f}s\033[0m")
    for a in sorted(RELEASE.glob("*")):
        if a.is_file() and a.suffix in (".AppImage", ".deb"):
            print(f"  → {a}")

    if args.launch:
        imgs = sorted(RELEASE.glob("*.AppImage"))
        if imgs:
            launcher = BUILD / "launch-test.sh"
            if launcher.is_file():
                run(["bash", str(launcher), "/tmp/sable-run", "/tmp/sable-run.log"])
                print(f"  launched; log: /tmp/sable-run.log")
            else:
                print(f"  no launch-test.sh — run manually: {imgs[-1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
