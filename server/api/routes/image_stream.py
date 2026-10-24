"""Streaming image generation.

The blocking ``POST /api/tool/generate_image`` returns nothing until the whole
batch finishes: no progress, no partial results, and nothing to abort. This
module adds ``POST /api/tool/generate_image_stream``, which runs the same tool
but forwards events as they happen over SSE:

    log    {"line": "..."}                    human-readable progress
    image  {"index": 0, "image": {...}}       one finished image
    done   {"ok": true, "result": {...}}      same payload as the old route
    error  {"error": "..."}

Cancellation is real: the subprocess is killed when the client disconnects, so
the Stop button aborts work in flight rather than just hiding the spinner.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..dependencies import sse

router = APIRouter()

_TOOLS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "tools"
_SCRIPT = _TOOLS_DIR / "image_generator" / "scripts" / "image_generator.py"

# Providers that run as the CLI script. The rest are single HTTP calls handled
# inline by the blocking route and have no per-image progress to stream.
# DreamForge / Advanced SDXL take a different flag set (art_style, batch_size,
# quality_level ...) and are served by their own dedicated endpoints. Adding
# them here without a matching _cli_args mapping would silently build wrong
# command-line arguments.
_SCRIPT_PROVIDERS = {"perchance"}

_STREAM_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # Stops reverse proxies buffering the stream into uselessness.
    "X-Accel-Buffering": "no",
}


def _cli_args(body: dict[str, Any]) -> list[str]:
    """Build the script arguments from the request body."""
    cli = ["generate"]
    param_map = {
        "prompt": "--prompt",
        "style": "--style",
        "shape": "--shape",
        "count": "--count",
        "negative_prompt": "--negative-prompt",
        "seed": "--seed",
        "key": "--key",
    }
    for key, flag in param_map.items():
        val = body.get(key)
        if val is not None and str(val) != "":
            cli += [flag, str(val)]
    return cli


async def _stream_script(body: dict[str, Any], request: Request) -> AsyncIterator[str]:
    """Run the CLI script and forward its output as it arrives."""
    if not _SCRIPT.is_file():
        yield sse({"type": "error", "error": "tool script not found"})
        return

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(_SCRIPT),
        *_cli_args(body),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    images: list[dict[str, Any]] = []
    killed = False

    async def pump_stderr() -> None:
        """Forward log lines; lift PROGRESS lines into image events."""
        assert proc.stderr is not None
        while True:
            raw = await proc.stderr.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            if line.startswith("PROGRESS "):
                try:
                    payload = json.loads(line[len("PROGRESS "):])
                except Exception:
                    continue
                img = payload.get("image") or {}
                idx = payload.get("index", len(images))
                images.append(img)
                queue.put_nowait({"type": "image", "index": idx, "image": img})
            else:
                queue.put_nowait({"type": "log", "line": line})

    async def watch_disconnect() -> None:
        """Kill the subprocess the moment the client goes away."""
        nonlocal killed
        while proc.returncode is None:
            if await request.is_disconnected():
                killed = True
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                return
            await asyncio.sleep(0.25)

    stderr_task = asyncio.create_task(pump_stderr())
    watch_task = asyncio.create_task(watch_disconnect())

    yield sse({"type": "log", "line": "starting %s..." % (body.get("provider") or "perchance")})

    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                # Nothing new; keep looping so a disconnect is noticed promptly
                # even when the tool is quiet (e.g. waiting on a key capture).
                if proc.returncode is not None and stderr_task.done():
                    break
                continue
            yield sse(item)

        await stderr_task

        stdout = b""
        if proc.stdout is not None:
            stdout = await proc.stdout.read()
        text = stdout.decode("utf-8", "replace").strip()

        if killed:
            yield sse({"type": "log", "line": "stopped by user"})
            return

        if not text:
            yield sse({"type": "error", "error": "tool produced no output"})
            return

        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            yield sse({"type": "error", "error": "tool produced non-JSON output"})
            return

        # Backfill anything the script did not report via PROGRESS, so this
        # endpoint stays correct even for a provider path without hooks.
        seen = {json.dumps(i, sort_keys=True) for i in images}
        for img in result.get("images") or []:
            if json.dumps(img, sort_keys=True) not in seen:
                images.append(img)
                yield sse({"type": "image", "index": len(images) - 1, "image": img})

        yield sse({"type": "done", "ok": bool(result.get("ok")), "result": result})
    finally:
        watch_task.cancel()
        stderr_task.cancel()
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass


@router.post("/api/tool/generate_image_stream")
async def generate_image_stream(request: Request) -> StreamingResponse:
    """Generate images, streaming progress, per-image results and logs."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    if not str(body.get("prompt") or "").strip():
        raise HTTPException(status_code=400, detail="prompt is required")

    provider = str(body.get("provider") or "perchance")
    if provider not in _SCRIPT_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail="provider '%s' does not support streaming" % provider,
        )

    return StreamingResponse(
        _stream_script(body, request),
        media_type="text/event-stream",
        headers=_STREAM_HEADERS,
    )
