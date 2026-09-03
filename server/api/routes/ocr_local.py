"""Local OCR provider management — install/uninstall/status + unified recognize endpoints.

Engine logic lives in ocr_engines/ (one file per provider).
This module handles routing, dependency management, and SSE streaming.
Dependencies are NOT in pyproject.toml — installed on-demand via pip into the venv.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)
router = APIRouter()

# ── Provider registry ──────────────────────────────────────────────

PROVIDERS: dict[str, dict[str, Any]] = {
    "banglaocr": {
        "name": "BanglaOCR",
        "type": "cloud",
        "description": "Cloud-based Bangla+English OCR via BanglaOCR API",
        "deps": [],
    },
    "sableocr": {
        "name": "SableOCR",
        "type": "local",
        "description": "Local Bangla+English OCR using Pytesseract + Unicode Normalizer",
        "deps": ["pytesseract", "opencv-python-headless", "bnunicodenormalizer"],
        "default_lang": "eng+ben",
    },
    "paddleocr": {
        "name": "PaddleOCR",
        "type": "local",
        "description": "Local OCR using PaddlePaddle + PaddleOCR models",
        "deps": ["paddlepaddle", "paddleocr", "opencv-python-headless"],
        "default_lang": "en",
    },
    "pytesseract": {
        "name": "Pytesseract",
        "type": "local",
        "description": "Raw Pytesseract OCR (no preprocessing or normalization)",
        "deps": ["pytesseract", "opencv-python-headless"],
        "default_lang": "eng",
    },
}


def _get_venv_pip_cmd() -> list[str]:
    """Return pip command using python -m pip (works even without pip script)."""
    return [sys.executable, "-m", "pip"]


def _is_package_installed(package: str) -> bool:
    try:
        result = subprocess.run(
            [sys.executable, "-c", f"import {package.replace('-', '_')}"],
            capture_output=True, timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


# ── Status / Install / Uninstall ───────────────────────────────────

@router.get("/api/ocr/providers")
async def list_providers() -> dict[str, Any]:
    result = {}
    for pid, info in PROVIDERS.items():
        if info["type"] == "cloud":
            result[pid] = {**info, "installed": True, "ready": True}
            continue
        deps_ok = all(_is_package_installed(d) for d in info["deps"])
        result[pid] = {
            **info,
            "installed": deps_ok,
            "ready": deps_ok,
        }
    return result


@router.post("/api/ocr/install/{provider_id}")
async def install_provider(provider_id: str) -> StreamingResponse:
    """SSE-streamed pip install with real-time progress."""
    import json as _json

    if provider_id not in PROVIDERS:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider_id}")

    info = PROVIDERS[provider_id]
    if info["type"] == "cloud":
        raise HTTPException(status_code=400, detail="Cloud provider needs no installation")
    if not info["deps"]:
        raise HTTPException(status_code=400, detail="No Python dependencies to install")

    cmd = _get_venv_pip_cmd() + ["install", "--progress-bar", "off"] + info["deps"]
    logger.info("Installing %s deps: %s", provider_id, " ".join(info["deps"]))

    async def _stream():
        yield f"data: {_json.dumps({'type': 'start', 'deps': info['deps']})}\n\n"

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        # Stream stdout line by line
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace").rstrip()
            if text:
                yield f"data: {_json.dumps({'type': 'log', 'text': text})}\n\n"

        await proc.wait()

        if proc.returncode != 0:
            yield f"data: {_json.dumps({'type': 'error', 'message': f'pip exited with code {proc.returncode}'})}\n\n"
            return

        done_msg = f"Installed {', '.join(info['deps'])}"
        yield f"data: {_json.dumps({'type': 'done', 'message': done_msg})}\n\n"

    return StreamingResponse(_stream(), media_type="text/event-stream")


@router.post("/api/ocr/uninstall/{provider_id}")
async def uninstall_provider(provider_id: str) -> dict[str, Any]:
    if provider_id not in PROVIDERS:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider_id}")

    info = PROVIDERS[provider_id]
    if info["type"] == "cloud":
        raise HTTPException(status_code=400, detail="Cannot uninstall cloud provider")
    if not info["deps"]:
        return {"status": "ok", "message": "Nothing to uninstall"}

    cmd = _get_venv_pip_cmd() + ["uninstall", "-y", "--quiet"] + info["deps"]
    logger.info("Uninstalling %s deps: %s", provider_id, " ".join(info["deps"]))

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    return {"status": "ok", "message": f"Uninstalled {', '.join(info['deps'])}"}


# ── Unified local recognize endpoints ─────────────────────────────

@router.post("/api/ocr/local/recognize")
async def local_ocr_recognize(
    file: UploadFile = File(...),
    provider: str = "sableocr",
    lang: str = "",
) -> dict[str, Any]:
    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider}")
    info = PROVIDERS[provider]
    if info["type"] == "cloud":
        raise HTTPException(status_code=400, detail="Use /api/ocr/recognize for cloud providers")
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")

    data = await file.read()
    language = lang or info.get("default_lang", "eng")

    from .ocr_engines import get_runner
    runner = get_runner(provider)

    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, runner, data, file.filename, language)
    except ImportError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Missing dependency: {exc}. Install via the provider settings.",
        ) from exc
    except Exception as exc:
        logger.error("Local OCR error (%s): %s", provider, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/api/ocr/local/stream")
async def local_ocr_stream(
    files: list[UploadFile] = File(...),
    provider: str = "sableocr",
    lang: str = "",
) -> StreamingResponse:
    import json as _json

    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider}")
    info = PROVIDERS[provider]
    if info["type"] == "cloud":
        raise HTTPException(status_code=400, detail="Use /api/ocr/stream for cloud providers")

    file_data: list[tuple[str, bytes]] = []
    for file in files:
        if not file.filename:
            continue
        data = await file.read()
        file_data.append((file.filename, data))

    total = len(file_data)
    language = lang or info.get("default_lang", "eng")

    from .ocr_engines import get_runner
    runner = get_runner(provider)

    async def _event_generator():
        yield f"data: {_json.dumps({'type': 'init', 'total_pages': total, 'total_files': total})}\n\n"

        results: list[dict[str, Any]] = []
        completed = 0
        loop = asyncio.get_event_loop()

        for fname, fdata in file_data:
            try:
                r = await loop.run_in_executor(None, runner, fdata, fname, language)
                results.append(r)
            except ImportError as exc:
                results.append({"error": f"Missing dependency: {exc}", "source_filename": fname})
            except Exception as exc:
                logger.error("Local OCR error for %s: %s", fname, exc)
                results.append({"error": str(exc), "source_filename": fname})

            completed += 1
            pct = round((completed / total) * 100) if total else 0
            yield f"data: {_json.dumps({'type': 'progress', 'pages_done': completed, 'total_pages': total, 'pct': pct})}\n\n"

        yield f"data: {_json.dumps({'type': 'complete', 'results': results})}\n\n"

    return StreamingResponse(_event_generator(), media_type="text/event-stream")
