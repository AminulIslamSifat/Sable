from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
import tomllib
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
_GITHUB_REPO = "AminulIslamSifat/Sable"
_GITHUB_API = f"https://api.github.com/repos/{_GITHUB_REPO}/releases/latest"
_GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
_CACHE_TTL = 1800  # 30 minutes

# Simple in-memory cache
_cache: dict[str, Any] = {"ts": 0, "data": None}


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
    # Match patterns like v1.0.0, 1.2.3, 0.4.0
    match = re.search(r'v?(\d+\.\d+\.\d+)', text)
    return match.group(1) if match else None


def _compare_versions(local: str, remote: str) -> bool:
    """Return True if remote > local."""
    try:
        # Try direct parse first, then extract from string
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


@router.get("/api/update/check")
def check_update(force: bool = False) -> dict[str, Any]:
    """Check GitHub Releases for a newer version."""
    if not force:
        cached = _check_cache()
        if cached:
            return cached

    local_version = _get_local_version()

    try:
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "Sable-UpdateChecker",
        }
        if _GITHUB_TOKEN:
            headers["Authorization"] = f"Bearer {_GITHUB_TOKEN}"

        resp = httpx.get(
            _GITHUB_API,
            headers=headers,
            timeout=15,
        )
        if resp.status_code == 404:
            # No releases yet
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

        resp.raise_for_status()
        release = resp.json()

    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="GitHub API timeout")
    except Exception as exc:
        logger.error("Update check failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"Update check failed: {exc}")

    remote_tag = release.get("tag_name", "")
    release_name = release.get("name", "")
    # Try tag first, fall back to extracting version from release name
    remote_version = _extract_version(remote_tag) or _extract_version(release_name) or remote_tag
    update_available = _compare_versions(local_version, remote_version)

    result = {
        "update_available": update_available,
        "local_version": local_version,
        "remote_version": remote_version,
        "changelog": release.get("body", ""),
        "published_at": release.get("published_at", ""),
        "release_name": release.get("name", remote_tag),
        "html_url": release.get("html_url", ""),
    }

    _cache.update(ts=time.time(), data=result)
    return result


@router.post("/api/update/apply")
async def apply_update() -> StreamingResponse:
    """Apply update: git pull + uv sync + restart service. Streams SSE progress."""

    async def generator():
        yield sse({"type": "progress", "step": "check", "message": "Checking for uncommitted changes…"})

        # Step 1: Check for uncommitted changes
        proc = await asyncio.to_thread(
            subprocess.run,
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, cwd=str(_PROJECT_ROOT),
        )
        if proc.returncode != 0:
            yield sse({"type": "error", "message": f"Git status failed: {proc.stderr.strip()}"})
            return

        dirty_files = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
        if dirty_files:
            yield sse({
                "type": "warning",
                "message": f"{len(dirty_files)} uncommitted change(s) detected. Stashing…",
            })
            stash = await asyncio.to_thread(
                subprocess.run,
                ["git", "stash", "push", "-m", "auto-stash before update"],
                capture_output=True, text=True, cwd=str(_PROJECT_ROOT),
            )
            if stash.returncode != 0:
                yield sse({"type": "error", "message": f"Git stash failed: {stash.stderr.strip()}"})
                return
            yield sse({"type": "log", "step": "check", "message": "Changes stashed."})

        # Helper: stream a subprocess line-by-line via SSE.
        # Uses a mutable list to smuggle the return code out of the async generator.
        async def _stream_cmd(cmd: list[str], step_id: str, timeout_sec: int = 120, rc_out: list[int] | None = None):
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(_PROJECT_ROOT),
            )
            try:
                while True:
                    line = await asyncio.wait_for(
                        process.stdout.readline(), timeout=timeout_sec
                    )
                    if not line:
                        break
                    decoded = line.decode("utf-8", errors="replace").rstrip()
                    if decoded:
                        yield sse({"type": "log", "step": step_id, "message": decoded})
            except asyncio.TimeoutError:
                process.kill()
                yield sse({"type": "error", "message": f"Timed out after {timeout_sec}s: {' '.join(cmd)}"})
                if rc_out is not None:
                    rc_out[0] = -1
                return

            await process.wait()
            if rc_out is not None:
                rc_out[0] = process.returncode

        # Step 2: Git fetch + merge via HTTPS
        yield sse({"type": "progress", "step": "pull", "message": "Fetching latest code…"})
        _https_url = f"https://github.com/{_GITHUB_REPO}.git"

        fetch_rc: list[int] = [0]
        async for event in _stream_cmd(
            ["git", "fetch", "--progress", _https_url, "main"], "pull", 120, fetch_rc
        ):
            yield event
        if fetch_rc[0] != 0:
            yield sse({"type": "error", "message": "Git fetch failed."})
            return

        yield sse({"type": "progress", "step": "pull", "message": "Merging…"})
        merge_rc: list[int] = [0]
        async for event in _stream_cmd(
            ["git", "merge", "FETCH_HEAD", "--no-edit"], "pull", 60, merge_rc
        ):
            yield event
        if merge_rc[0] != 0:
            yield sse({"type": "error", "message": "Git merge failed."})
            return
        yield sse({"type": "log", "step": "pull", "message": "Code updated ✓"})

        # Step 3: uv sync
        yield sse({"type": "progress", "step": "sync", "message": "Syncing dependencies…"})
        sync_rc: list[int] = [0]
        async for event in _stream_cmd(
            ["uv", "sync"], "sync", 180, sync_rc
        ):
            yield event
        if sync_rc[0] != 0:
            yield sse({"type": "error", "message": "uv sync failed."})
            return
        yield sse({"type": "log", "step": "sync", "message": "Dependencies synced ✓"})

        # Step 4: Restart service
        yield sse({"type": "progress", "step": "restart", "message": "Restarting service… (page will reload)"})
        await asyncio.sleep(0.5)

        from engine.service_manager import restart_service as _restart
        _restart()

        yield sse({"type": "done", "message": "Update complete. Restarting…"})

    return StreamingResponse(generator(), media_type="text/event-stream")
