from __future__ import annotations

import asyncio
import os
import platform
import re
import shutil

import tempfile
import time
import tomllib
import zipfile
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from packaging.version import Version, InvalidVersion

from server.utils import logger
from ..dependencies import sse

router = APIRouter()

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_PYPROJECT = _PROJECT_ROOT / "pyproject.toml"

# ── Release source ─────────────────────────────────────────────
# Zip packages are published as GitHub Release assets on Sable.
# That repo is the public download source and holds NO application source.
# The code itself lives in Sable-Core (private).
_RELEASE_REPO = "AminulIslamSifat/Sable"
_GITHUB_API = f"https://api.github.com/repos/{_RELEASE_REPO}/releases/latest"
_GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
_CACHE_TTL = 1800  # 30 minutes

_cache: dict[str, Any] = {"ts": 0, "data": None}


# ── Never overwritten by an update ─────────────────────────────
_PROTECTED_RELPATHS = {
    "instruction/Maria.md",
    "instruction/personal.md",
    "Brain/Memory.json",
    "Brain/Protected.json",
    "Brain/Procedural.json",
    "Brain/user_personality.json",
    "Brain/disabled_tools.json",
    "Brain/disabled_skills.json",
    ".auth_token",
    "system/.auth_token",
}

_PROTECTED_PREFIXES = ("system/", "output/", ".venv/", ".git/", ".sable_backups/")


# ── Version helpers ────────────────────────────────────────────

def _get_local_version() -> str:
    """Read version from pyproject.toml."""
    try:
        with open(_PYPROJECT, "rb") as f:
            data = tomllib.load(f)
        return data.get("project", {}).get("version", "0.0.0")
    except Exception as exc:
        logger.warning("Failed to read local version: %s", exc)
        return "0.0.0"


def _extract_version(text: str) -> str | None:
    """Extract a semver-like version from a string (tag or release name)."""
    match = re.search(r'v?(\d+\.\d+\.\d+)', text)
    return match.group(1) if match else None


def _compare_versions(local: str, remote: str) -> bool:
    """Return True if remote > local."""
    try:
        try:
            remote_ver = Version(remote.lstrip("v"))
        except InvalidVersion:
            extracted = _extract_version(remote)
            if not extracted:
                return False
            remote_ver = Version(extracted)
        return remote_ver > Version(local)
    except InvalidVersion:
        return False


def _check_cache() -> dict[str, Any] | None:
    if _cache["data"] and (time.time() - _cache["ts"]) < _CACHE_TTL:
        return _cache["data"]
    return None


# ── Platform / asset mapping ───────────────────────────────────

def _platform_tag() -> str | None:
    """Return the package platform tag for the running OS, or None."""
    system = platform.system().lower()
    if system == "windows":
        return "windows"
    if system == "linux":
        return "linux"
    if system == "darwin":
        return "macos"
    return None


def _pick_asset(release: dict[str, Any], version: str) -> dict[str, Any] | None:
    """Find the release asset matching this platform."""
    tag = _platform_tag()
    if not tag:
        return None
    want = f"sable-{version}-{tag}.zip"
    assets = release.get("assets") or []
    for asset in assets:
        if asset.get("name") == want:
            return asset
    # Fallback only if the name carries BOTH this version and this platform.
    # Never match on platform alone — that would install the wrong version.
    for asset in assets:
        name = asset.get("name", "")
        if f"-{version}-" in name and name.endswith(f"-{tag}.zip"):
            return asset
    return None


# ── Release lookup ─────────────────────────────────────────────

def _fetch_latest_release() -> dict[str, Any]:
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Sable-UpdateChecker",
    }
    if _GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {_GITHUB_TOKEN}"
    resp = httpx.get(_GITHUB_API, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()


@router.get("/api/update/check")
def check_update(force: bool = False) -> dict[str, Any]:
    """Check the release repo for a newer packaged version."""
    if not force:
        cached = _check_cache()
        if cached:
            return cached

    local_version = _get_local_version()
    platform_tag = _platform_tag()

    try:
        release = _fetch_latest_release()
    except httpx.HTTPStatusError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            result = {
                "update_available": False,
                "local_version": local_version,
                "remote_version": local_version,
                "changelog": "",
                "published_at": "",
                "message": "No releases published yet.",
            }
            _cache.update(ts=time.time(), data=result)
            return result
        logger.error("Update check failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"Update check failed: {exc}")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="GitHub API timeout")
    except Exception as exc:
        logger.error("Update check failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"Update check failed: {exc}")

    remote_tag = release.get("tag_name", "")
    release_name = release.get("name", "")
    remote_version = _extract_version(remote_tag) or _extract_version(release_name) or remote_tag
    update_available = _compare_versions(local_version, remote_version)

    asset = _pick_asset(release, remote_version)
    if update_available and not asset and platform_tag:
        logger.warning("No %s asset found for v%s", platform_tag, remote_version)

    result = {
        "update_available": bool(update_available and asset is not None),
        "local_version": local_version,
        "remote_version": remote_version,
        "changelog": release.get("body", ""),
        "published_at": release.get("published_at", ""),
        "release_name": release.get("name", remote_tag),
        "html_url": release.get("html_url", ""),
        "platform": platform_tag or "unsupported",
        "asset": asset.get("name") if asset else "",
        "asset_size": asset.get("size", 0) if asset else 0,
    }
    _cache.update(ts=time.time(), data=result)
    return result


# ── Safe extraction ────────────────────────────────────────────

def _is_protected(rel_posix: str) -> bool:
    if rel_posix in _PROTECTED_RELPATHS:
        return True
    return any(rel_posix.startswith(p) for p in _PROTECTED_PREFIXES)


def _safe_extract(zip_path: Path, dest: Path) -> int:
    """Extract a zip into dest, rejecting path traversal and protected paths."""
    dest = dest.resolve()
    written = 0
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            name = member.filename
            if not name or name.endswith("/"):
                continue

            target = (dest / name).resolve()
            try:
                target.relative_to(dest)
            except ValueError:
                logger.warning("Skipping unsafe zip member: %s", name)
                continue

            rel_posix = target.relative_to(dest).as_posix()
            if _is_protected(rel_posix):
                logger.info("Preserving user file (skipped from update): %s", rel_posix)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)

            mode = (member.external_attr >> 16) & 0o777
            if mode:
                try:
                    target.chmod(mode)
                except OSError:
                    pass
            written += 1
    return written



def _plan_overlay(stage: Path, dst: Path) -> list[str]:
    """Return the relative paths that an overlay of `stage` would write."""
    planned: list[str] = []
    for root, dirs, files in os.walk(stage):
        root_p = Path(root)
        rel_root = root_p.relative_to(stage)
        dirs[:] = [d for d in dirs if d not in {".git", ".venv", "__pycache__"}]
        for fn in files:
            rel = (rel_root / fn).as_posix()
            if _is_protected(rel):
                continue
            planned.append(rel)
    return planned


def _backup_files(rels: list[str], dst: Path, backup_dir: Path) -> list[dict[str, Any]]:
    """Snapshot every file about to be overwritten.

    Returns a journal of entries: {rel, existed, backup}. Files that don't yet
    exist are recorded with existed=False so rollback can delete them again.
    """
    dst = dst.resolve()
    journal: list[dict[str, Any]] = []
    for rel in rels:
        target = dst / rel
        entry: dict[str, Any] = {"rel": rel, "existed": target.exists(), "backup": None}
        if entry["existed"]:
            bpath = backup_dir / rel
            bpath.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, bpath)
            entry["backup"] = str(bpath)
        journal.append(entry)
    return journal


def _restore_files(journal: list[dict[str, Any]], dst: Path) -> tuple[int, int]:
    """Restore a snapshot. Returns (restored, removed)."""
    dst = dst.resolve()
    restored = 0
    removed = 0
    for entry in journal:
        target = dst / entry["rel"]
        try:
            if entry["existed"] and entry["backup"]:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(entry["backup"], target)
                restored += 1
            elif not entry["existed"] and target.exists():
                target.unlink()
                removed += 1
        except OSError as exc:
            logger.warning("Rollback failed for %s: %s", entry["rel"], exc)
    return restored, removed


async def _rollback(journal: list[dict[str, Any]]):
    """SSE generator that restores a snapshot."""
    if not journal:
        yield sse({"type": "error", "message": "Rollback FAILED: no snapshot available — manual fix needed."})
        return
    try:
        restored, removed = await asyncio.to_thread(_restore_files, journal, _PROJECT_ROOT)
    except Exception as exc:
        yield sse({"type": "error", "message": f"Rollback FAILED: {exc} — manual fix needed."})
        return
    yield sse({
        "type": "log", "step": "rollback",
        "message": f"Rolled back: {restored} files restored, {removed} new files removed ✓",
    })


def _overlay_tree(src: Path, dst: Path) -> int:
    """Copy every file from src over dst. Never deletes anything in dst."""
    dst = dst.resolve()
    copied = 0
    for root, dirs, files in os.walk(src):
        root_p = Path(root)
        rel_root = root_p.relative_to(src)
        dirs[:] = [d for d in dirs if d not in {".git", ".venv", "__pycache__"}]

        for fn in files:
            src_file = root_p / fn
            rel = (rel_root / fn).as_posix()
            if _is_protected(rel):
                continue
            target = dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(src_file, target)
            except OSError as exc:
                if "Text file busy" in str(exc) or getattr(exc, "errno", None) == 26:
                    time.sleep(1)
                    shutil.copy2(src_file, target)
                else:
                    raise
            copied += 1
    return copied


# ── Apply ──────────────────────────────────────────────────────

async def _stream_cmd(cmd: list[str], step_id: str, timeout_sec: int, rc_out: list[int]):
    """Run a subprocess, yield SSE log events, write exit code into rc_out[0]."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "echo"
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(_PROJECT_ROOT),
            env=env,
        )
    except FileNotFoundError:
        yield sse({"type": "warning", "message": f"Command not found: {cmd[0]}"})
        rc_out[0] = 127
        return

    started = time.time()
    while True:
        if time.time() - started > timeout_sec:
            process.kill()
            yield sse({"type": "error", "message": f"Timed out after {timeout_sec}s: {' '.join(cmd)}"})
            rc_out[0] = -1
            return
        try:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=5)
        except asyncio.TimeoutError:
            continue
        if not line:
            break
        decoded = line.decode("utf-8", errors="replace").rstrip()
        if decoded:
            yield sse({"type": "log", "step": step_id, "message": decoded})

    await process.wait()
    rc_out[0] = process.returncode


@router.post("/api/update/apply")
async def apply_update() -> StreamingResponse:
    """Download the platform zip, extract over the install, sync deps, restart.

    Never touches system/, output/, .venv/, or personal config/memory files.
    Downloads and stages first so a corrupt package fails before the live
    install is modified.
    """

    async def generator():
        local_version = _get_local_version()
        platform_tag = _platform_tag()

        if not platform_tag:
            yield sse({"type": "error", "message": f"Unsupported platform: {platform.system()}"})
            return

        # ── Step 1: Resolve release + asset ──────────────────────────────
        yield sse({"type": "progress", "step": "check", "message": "Checking for updates…"})
        try:
            release = await asyncio.to_thread(_fetch_latest_release)
        except Exception as exc:
            yield sse({"type": "error", "message": f"Could not reach release repo: {exc}"})
            return

        remote_tag = release.get("tag_name", "")
        remote_version = (
            _extract_version(remote_tag)
            or _extract_version(release.get("name", ""))
            or remote_tag
        )
        if not _compare_versions(local_version, remote_version):
            yield sse({"type": "log", "step": "check", "message": f"Already on latest (v{local_version})."})
            yield sse({"type": "done", "message": "Nothing to update."})
            return

        asset = _pick_asset(release, remote_version)
        if not asset:
            yield sse({"type": "error", "message": f"Release v{remote_version} has no {platform_tag} package."})
            return

        asset_url = asset.get("browser_download_url", "")
        asset_name = asset.get("name", "")
        yield sse({"type": "log", "step": "check", "message": f"Found v{remote_version} ({asset_name})"})

        # ── Step 2: Download to temp ─────────────────────────────────────
        yield sse({"type": "progress", "step": "pull", "message": f"Downloading {asset_name}…"})
        tmp_dir = Path(tempfile.mkdtemp(prefix="sable-update-"))
        tmp_zip = tmp_dir / asset_name
        try:
            headers = {"User-Agent": "Sable-Updater"}
            if _GITHUB_TOKEN:
                headers["Authorization"] = f"Bearer {_GITHUB_TOKEN}"

            async with httpx.AsyncClient(follow_redirects=True, timeout=300) as client:
                async with client.stream("GET", asset_url, headers=headers) as resp:
                    resp.raise_for_status()
                    total = int(resp.headers.get("content-length", 0)) or asset.get("size", 0)
                    got = 0
                    last_report = 0.0
                    with open(tmp_zip, "wb") as fh:
                        async for chunk in resp.aiter_bytes(1024 * 256):
                            fh.write(chunk)
                            got += len(chunk)
                            now = time.time()
                            if total and now - last_report > 0.5:
                                last_report = now
                                pct = got * 100 // total
                                yield sse({
                                    "type": "log", "step": "pull",
                                    "message": f"  {got/1048576:.1f} / {total/1048576:.1f} MB ({pct}%)",
                                })
        except Exception as exc:
            yield sse({"type": "error", "message": f"Download failed: {exc}"})
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return
        yield sse({"type": "log", "step": "pull", "message": f"Downloaded {tmp_zip.stat().st_size/1048576:.1f} MB ✓"})

        # ── Step 3: Staging extract (validate before touching install) ───
        stage = tmp_dir / "stage"
        try:
            count = await asyncio.to_thread(_safe_extract, tmp_zip, stage)
        except zipfile.BadZipFile as exc:
            yield sse({"type": "error", "message": f"Corrupt package: {exc}"})
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return
        except Exception as exc:
            yield sse({"type": "error", "message": f"Extract failed: {exc}"})
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return

        if count == 0:
            yield sse({"type": "error", "message": "Package was empty — aborting."})
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return
        yield sse({"type": "log", "step": "pull", "message": f"Staged {count} files ✓"})

        # ── Step 4: Snapshot everything we're about to overwrite ─────────
        yield sse({"type": "progress", "step": "pull", "message": "Snapshotting current install…"})
        backup_dir = tmp_dir / "backup"
        try:
            planned = await asyncio.to_thread(_plan_overlay, stage, _PROJECT_ROOT)
            journal = await asyncio.to_thread(_backup_files, planned, _PROJECT_ROOT, backup_dir)
        except Exception as exc:
            yield sse({"type": "error", "message": f"Snapshot failed: {exc} — aborting before touching install."})
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return
        to_overwrite = sum(1 for e in journal if e["existed"])
        yield sse({
            "type": "log", "step": "pull",
            "message": f"Snapshot: {len(journal)} files ({to_overwrite} to overwrite, {len(journal) - to_overwrite} new) ✓",
        })

        # ── Step 5: Install (overlay staged tree) ────────────────────────
        yield sse({"type": "progress", "step": "pull", "message": "Installing update…"})
        try:
            copied = await asyncio.to_thread(_overlay_tree, stage, _PROJECT_ROOT)
        except Exception as exc:
            yield sse({"type": "error", "message": f"Install failed mid-copy: {exc} — rolling back…"})
            async for ev in _rollback(journal):
                yield ev
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return
        yield sse({"type": "log", "step": "pull", "message": f"Installed {copied} files ✓"})

        # ── Step 6: uv sync — rollback if it fails ───────────────────────
        yield sse({"type": "progress", "step": "sync", "message": "Syncing dependencies…"})
        sync_rc: list[int] = [0]
        async for ev in _stream_cmd(["uv", "sync"], "sync", 300, sync_rc):
            yield ev
        if sync_rc[0] != 0:
            yield sse({"type": "warning", "message": "uv sync failed — rolling back to previous version…"})
            async for ev in _rollback(journal):
                yield ev
            shutil.rmtree(tmp_dir, ignore_errors=True)
            yield sse({
                "type": "error",
                "message": "Update aborted and rolled back. Service still running on the old version.",
            })
            return
        yield sse({"type": "log", "step": "sync", "message": "Dependencies synced ✓"})

        shutil.rmtree(tmp_dir, ignore_errors=True)

        # ── Step 7: Restart ──────────────────────────────────────────────
        # Point of no return: restart_service() calls os._exit(0), so nothing
        # after this line runs and rollback is no longer possible. Reaching here
        # means snapshot + overlay + uv sync all succeeded.
        yield sse({"type": "progress", "step": "restart", "message": "Restarting service… (page will reload)"})
        await asyncio.sleep(0.5)
        try:
            from engine.service_manager import restart_service as _restart
            _restart()
        except Exception as exc:
            yield sse({"type": "warning", "message": f"Auto-restart failed ({exc}) — restart Sable manually."})

        yield sse({"type": "done", "message": f"Updated to v{remote_version}. Restarting…"})

    return StreamingResponse(generator(), media_type="text/event-stream")
