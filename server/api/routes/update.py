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


def _auth_url() -> str:
    """Return an authenticated HTTPS URL for the repo if a token is available."""
    if _GITHUB_TOKEN:
        return f"https://x-access-token:{_GITHUB_TOKEN}@github.com/{_GITHUB_REPO}.git"
    return f"https://github.com/{_GITHUB_REPO}.git"


def _run_git(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    """Blocking git helper. Always runs in project root, never prompts."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "echo"
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=str(_PROJECT_ROOT),
        timeout=timeout,
        env=env,
    )


def _current_branch() -> str:
    """Return current branch name, fallback to main."""
    try:
        proc = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], timeout=10)
        branch = proc.stdout.strip()
        if branch and branch != "HEAD":
            return branch
    except Exception:
        pass
    return "main"


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

        resp = httpx.get(_GITHUB_API, headers=headers, timeout=15)
        if resp.status_code == 404:
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
    """Force-update: fetch origin, hard-reset to origin branch, sync deps, restart.

    This is intentionally destructive: local changes are discarded, no merge is
    attempted, and no stash is created. Failures roll back to ORIG_HEAD.
    """

    async def generator():
        branch = _current_branch()
        remote_url = _auth_url()

        # Record pre-update HEAD so we can roll back if the rest of the update fails.
        pre_head_proc = await asyncio.to_thread(_run_git, ["rev-parse", "HEAD"], 15)
        pre_head = pre_head_proc.stdout.strip() if pre_head_proc.returncode == 0 else ""

        async def _stream_cmd(cmd: list[str], step_id: str, timeout_sec: int = 180, rc_out: list[int] | None = None):
            """Stream subprocess output line-by-line over SSE.

            Uses a mutable rc_out list so the caller can read the exit code
            after the async generator completes.
            """
            env = os.environ.copy()
            env["GIT_TERMINAL_PROMPT"] = "0"
            env["GIT_ASKPASS"] = "echo"
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(_PROJECT_ROOT),
                env=env,
            )
            started = time.time()
            try:
                while True:
                    if time.time() - started > timeout_sec:
                        process.kill()
                        yield sse({"type": "error", "message": f"Timed out after {timeout_sec}s: {' '.join(cmd)}"})
                        if rc_out is not None:
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
            except Exception as exc:
                process.kill()
                yield sse({"type": "error", "message": f"Command error: {exc}"})
                if rc_out is not None:
                    rc_out[0] = -1
                return

            await process.wait()
            if rc_out is not None:
                rc_out[0] = process.returncode

        async def _rollback(reason: str):
            """Hard-reset back to pre-update HEAD. Best-effort."""
            if not pre_head:
                yield sse({"type": "error", "message": f"{reason} (no rollback target)"})
                return
            yield sse({"type": "warning", "message": f"{reason} — rolling back to {pre_head[:8]}…"})
            rb_rc: list[int] = [0]
            async for ev in _stream_cmd(["git", "reset", "--hard", pre_head], "rollback", 60, rb_rc):
                yield ev
            if rb_rc[0] == 0:
                yield sse({"type": "error", "message": f"{reason}. Rolled back to previous version."})
            else:
                yield sse({"type": "error", "message": f"{reason}. Rollback ALSO failed — manual fix needed."})

        # ── Step 1: Fetch (force, tags included, shallow-safe) ───────────────
        yield sse({"type": "progress", "step": "pull", "message": f"Fetching origin/{branch}…"})
        fetch_rc: list[int] = [0]
        async for event in _stream_cmd(
            ["git", "fetch", "--force", "--prune", "--tags", remote_url, branch],
            "pull", 180, fetch_rc,
        ):
            yield event
        if fetch_rc[0] != 0:
            yield sse({"type": "error", "message": "Git fetch failed."})
            return

        # ── Step 2: Hard reset to FETCH_HEAD (discards ALL local changes) ────
        yield sse({"type": "progress", "step": "pull", "message": "Force-resetting working tree…"})
        # Clean untracked files that would block checkout, but keep ignored files (venv, data, etc.)
        clean_rc: list[int] = [0]
        async for event in _stream_cmd(["git", "clean", "-fd"], "pull", 60, clean_rc):
            yield event
        if clean_rc[0] != 0:
            yield sse({"type": "warning", "message": "git clean failed — continuing anyway."})

        reset_rc: list[int] = [0]
        async for event in _stream_cmd(["git", "reset", "--hard", "FETCH_HEAD"], "pull", 60, reset_rc):
            yield event
        if reset_rc[0] != 0:
            async for ev in _rollback("Git reset failed"):
                yield ev
            return
        yield sse({"type": "log", "step": "pull", "message": "Code updated (forced) ✓"})

        # ── Step 3: uv sync ──────────────────────────────────────────────────
        yield sse({"type": "progress", "step": "sync", "message": "Syncing dependencies…"})
        sync_rc: list[int] = [0]
        async for event in _stream_cmd(["uv", "sync"], "sync", 300, sync_rc):
            yield event
        if sync_rc[0] != 0:
            async for ev in _rollback("uv sync failed"):
                yield ev
            return
        yield sse({"type": "log", "step": "sync", "message": "Dependencies synced ✓"})

        # ── Step 4: Restart ──────────────────────────────────────────────────
        yield sse({"type": "progress", "step": "restart", "message": "Restarting service… (page will reload)"})
        await asyncio.sleep(0.5)

        from engine.service_manager import restart_service as _restart
        _restart()

        yield sse({"type": "done", "message": "Update complete. Restarting…"})

    return StreamingResponse(generator(), media_type="text/event-stream")
