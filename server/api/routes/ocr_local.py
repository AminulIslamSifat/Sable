"""Local OCR providers — SableOCR (pytesseract+bnunicode), PaddleOCR, raw Pytesseract.

Each provider has install/uninstall/status endpoints and a unified recognize endpoint.
Dependencies are NOT in pyproject.toml — installed on-demand via pip into the venv.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
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
        "system_deps": [],
    },
    "sableocr": {
        "name": "SableOCR",
        "type": "local",
        "description": "Local Bangla+English OCR using Pytesseract + Unicode Normalizer",
        "deps": ["pytesseract", "opencv-python-headless", "bnunicodenormalizer"],
        "system_deps": ["tesseract-ocr", "tesseract-ocr-ben"],
        "default_lang": "eng+ben",
    },
    "paddleocr": {
        "name": "PaddleOCR",
        "type": "local",
        "description": "Local OCR using PaddlePaddle + PaddleOCR models",
        "deps": ["paddlepaddle", "paddleocr", "opencv-python-headless"],
        "system_deps": [],
        "default_lang": "en",
    },
    "pytesseract": {
        "name": "Pytesseract",
        "type": "local",
        "description": "Raw Pytesseract OCR (no preprocessing or normalization)",
        "deps": ["pytesseract", "opencv-python-headless"],
        "system_deps": ["tesseract-ocr"],
        "default_lang": "eng",
    },
}


def _get_venv_pip() -> str:
    """Get the pip executable for the current venv."""
    return str(Path(sys.executable).parent / "pip")


def _is_package_installed(package: str) -> bool:
    """Check if a Python package is installed in the current venv."""
    try:
        result = subprocess.run(
            [sys.executable, "-c", f"import {package.replace('-', '_')}"],
            capture_output=True, timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def _is_system_dep_installed(dep: str) -> bool:
    """Check if a system dependency is available."""
    return shutil.which(dep) is not None


# ── Status / Install / Uninstall ───────────────────────────────────

@router.get("/api/ocr/providers")
async def list_providers() -> dict[str, Any]:
    """List all providers with their install status."""
    result = {}
    for pid, info in PROVIDERS.items():
        if info["type"] == "cloud":
            result[pid] = {**info, "installed": True, "ready": True}
            continue

        deps_ok = all(_is_package_installed(d) for d in info["deps"])
        sys_ok = all(_is_system_dep_installed(d) for d in info["system_deps"])
        result[pid] = {
            **info,
            "installed": deps_ok,
            "system_deps_met": sys_ok,
            "ready": deps_ok and sys_ok,
        }
    return result


@router.post("/api/ocr/install/{provider_id}")
async def install_provider(provider_id: str) -> dict[str, Any]:
    """Install Python dependencies for a local OCR provider."""
    if provider_id not in PROVIDERS:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider_id}")

    info = PROVIDERS[provider_id]
    if info["type"] == "cloud":
        return {"status": "ok", "message": "Cloud provider needs no installation"}

    if not info["deps"]:
        return {"status": "ok", "message": "No Python dependencies to install"}

    pip = _get_venv_pip()
    cmd = [pip, "install", "--quiet"] + info["deps"]

    logger.info("Installing %s deps: %s", provider_id, " ".join(info["deps"]))
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        err_msg = stderr.decode().strip()[-500:]
        logger.error("Install failed for %s: %s", provider_id, err_msg)
        raise HTTPException(status_code=500, detail=f"Install failed: {err_msg}")

    # Check system deps
    missing_sys = [d for d in info["system_deps"] if not _is_system_dep_installed(d)]
    msg = f"Installed {', '.join(info['deps'])}"
    if missing_sys:
        msg += f". WARNING: System deps missing: {', '.join(missing_sys)} (install via pacman/apt)"

    return {"status": "ok", "message": msg}


@router.post("/api/ocr/uninstall/{provider_id}")
async def uninstall_provider(provider_id: str) -> dict[str, Any]:
    """Uninstall Python dependencies for a local OCR provider."""
    if provider_id not in PROVIDERS:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider_id}")

    info = PROVIDERS[provider_id]
    if info["type"] == "cloud":
        raise HTTPException(status_code=400, detail="Cannot uninstall cloud provider")

    if not info["deps"]:
        return {"status": "ok", "message": "Nothing to uninstall"}

    pip = _get_venv_pip()
    cmd = [pip, "uninstall", "-y", "--quiet"] + info["deps"]

    logger.info("Uninstalling %s deps: %s", provider_id, " ".join(info["deps"]))
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()

    return {"status": "ok", "message": f"Uninstalled {', '.join(info['deps'])}"}


# ── Local OCR Engines ──────────────────────────────────────────────

def _run_sableocr(image_bytes: bytes, filename: str, lang: str) -> dict[str, Any]:
    """SableOCR: preprocess → dual OCR (gray+adaptive) → merge → normalize."""
    import cv2
    import numpy as np
    import pytesseract as pts
    from bnunicodenormalizer import Normalizer

    normalizer = Normalizer()

    # Decode image
    buf = np.frombuffer(image_bytes, np.uint8)
    img_cv = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if img_cv is None:
        raise ValueError(f"Could not decode image: {filename}")

    orig_h, orig_w = img_cv.shape[:2]

    # Preprocess: upscale 3x, grayscale, denoise, adaptive threshold
    scale = 3
    img_up = cv2.resize(img_cv, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(img_up, cv2.COLOR_BGR2GRAY)
    denoised = cv2.fastNlMeansDenoising(gray, None, h=10, templateWindowSize=7, searchWindowSize=21)
    adaptive = cv2.adaptiveThreshold(
        denoised, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 51, 10
    )
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    adaptive = cv2.morphologyEx(adaptive, cv2.MORPH_CLOSE, kernel)

    def extract_words(image, lang_str, sc):
        data = pts.image_to_data(image, lang=lang_str, output_type=pts.Output.DICT, config="--psm 6")
        words = []
        for i in range(len(data["text"])):
            text = data["text"][i].strip()
            if not text:
                continue
            conf = data["conf"][i]
            h = data["height"][i]
            if conf < 25 or h < 18:
                continue
            r = normalizer(text)
            normalized = (r["normalized"] if r else None) or text
            words.append({
                "text": normalized,
                "left": data["left"][i] // sc,
                "top": data["top"][i] // sc,
                "width": data["width"][i] // sc,
                "height": h // sc,
                "conf": conf,
            })
        return words

    words_gray = extract_words(denoised, lang, scale)
    words_adapt = extract_words(adaptive, lang, scale)

    # Merge: prefer higher confidence at overlapping positions
    merged = list(words_gray)
    for wb in words_adapt:
        dominated = False
        for wa in merged:
            y_close = abs(wa["top"] - wb["top"]) <= 8
            x_overlap = not (wa["left"] + wa["width"] < wb["left"] or
                            wb["left"] + wb["width"] < wa["left"])
            if y_close and x_overlap:
                if wb["conf"] > wa["conf"]:
                    wa.update(wb)
                dominated = True
                break
        if not dominated:
            merged.append(wb)

    merged.sort(key=lambda w: (w["top"], w["left"]))

    # Build text lines from sorted words
    lines: list[list[dict]] = []
    current_line: list[dict] = []
    last_top = -999
    for w in merged:
        if abs(w["top"] - last_top) > 15:
            if current_line:
                lines.append(current_line)
            current_line = [w]
        else:
            current_line.append(w)
        last_top = w["top"]
    if current_line:
        lines.append(current_line)

    full_text = "\n".join(" ".join(w["text"] for w in line) for line in lines)

    return {
        "full_text": full_text,
        "word_count": len(merged),
        "source_filename": filename,
        "engine": "sableocr",
    }


def _run_pytesseract_raw(image_bytes: bytes, filename: str, lang: str) -> dict[str, Any]:
    """Raw pytesseract — no preprocessing, no normalization."""
    import cv2
    import numpy as np
    import pytesseract as pts

    buf = np.frombuffer(image_bytes, np.uint8)
    img_cv = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if img_cv is None:
        raise ValueError(f"Could not decode image: {filename}")

    text = pts.image_to_string(img_cv, lang=lang, config="--psm 6")
    return {
        "full_text": text.strip(),
        "source_filename": filename,
        "engine": "pytesseract",
    }


def _run_paddleocr(image_bytes: bytes, filename: str, lang: str) -> dict[str, Any]:
    """PaddleOCR local inference."""
    import os
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    os.environ["FLAGS_eager_delete_tensor_gb"] = "0.0"
    os.environ["FLAGS_fast_eager_deletion_mode"] = "True"

    import cv2
    import numpy as np
    from paddleocr import PaddleOCR

    buf = np.frombuffer(image_bytes, np.uint8)
    img_cv = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if img_cv is None:
        raise ValueError(f"Could not decode image: {filename}")

    ocr = PaddleOCR(
        use_angle_cls=False,
        lang=lang,
        use_gpu=False,
        det_limit_side_len=1024,
        cpu_threads=min((os.cpu_count() or 1), 4),
        ir_optim=True,
        layout=False,
        table=False,
        formula=False,
    )

    output = ocr.ocr(img_cv, cls=False)
    texts: list[str] = []
    if output and output[0]:
        for line in output[0]:
            if line and isinstance(line, list) and len(line) >= 2:
                text = line[1][0]
                score = float(line[1][1])
                if score > 0.5:
                    texts.append(text)

    return {
        "full_text": "\n".join(texts),
        "line_count": len(texts),
        "source_filename": filename,
        "engine": "paddleocr",
    }


# ── Unified local recognize endpoint ───────────────────────────────

@router.post("/api/ocr/local/recognize")
async def local_ocr_recognize(
    file: UploadFile = File(...),
    provider: str = "sableocr",
    lang: str = "",
) -> dict[str, Any]:
    """Run local OCR on a single uploaded image."""
    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider}")

    info = PROVIDERS[provider]
    if info["type"] == "cloud":
        raise HTTPException(status_code=400, detail="Use /api/ocr/recognize for cloud providers")

    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")

    data = await file.read()
    language = lang or info.get("default_lang", "eng")

    loop = asyncio.get_event_loop()
    try:
        if provider == "sableocr":
            result = await loop.run_in_executor(None, _run_sableocr, data, file.filename, language)
        elif provider == "pytesseract":
            result = await loop.run_in_executor(None, _run_pytesseract_raw, data, file.filename, language)
        elif provider == "paddleocr":
            result = await loop.run_in_executor(None, _run_paddleocr, data, file.filename, language)
        else:
            raise HTTPException(status_code=400, detail=f"Provider {provider} not implemented")
        return result
    except ImportError as exc:
        raise HTTPException(status_code=500, detail=f"Missing dependency: {exc}. Install via the provider settings.") from exc
    except Exception as exc:
        logger.error("Local OCR error (%s): %s", provider, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/api/ocr/local/stream")
async def local_ocr_stream(
    files: list[UploadFile] = File(...),
    provider: str = "sableocr",
    lang: str = "",
) -> StreamingResponse:
    """SSE endpoint for local OCR with progress."""
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

    async def _event_generator():
        yield f"data: {_json.dumps({'type': 'init', 'total_pages': total, 'total_files': total})}\n\n"

        results: list[dict[str, Any]] = []
        completed = 0
        loop = asyncio.get_event_loop()

        for fname, fdata in file_data:
            try:
                if provider == "sableocr":
                    r = await loop.run_in_executor(None, _run_sableocr, fdata, fname, language)
                elif provider == "pytesseract":
                    r = await loop.run_in_executor(None, _run_pytesseract_raw, fdata, fname, language)
                elif provider == "paddleocr":
                    r = await loop.run_in_executor(None, _run_paddleocr, fdata, fname, language)
                else:
                    r = {"error": f"Unknown provider: {provider}", "source_filename": fname}
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
