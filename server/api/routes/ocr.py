"""OCR route — proxies to BanglaOCR API with parallel PDF page splitting."""
from __future__ import annotations

import asyncio
import io
import logging
from typing import Any

import httpx
from fastapi import APIRouter, File, HTTPException, UploadFile

logger = logging.getLogger(__name__)

router = APIRouter()

BANGLA_OCR_URL = "https://api.banglaocr.com/api/public/ocr"
OCR_HEADERS = {
    "Referer": "https://banglaocr.com/",
    "Origin": "https://banglaocr.com/",
}
ALLOWED_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tiff"}
ALLOWED_PDF_EXT = {".pdf"}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB per file

_MIME_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".tiff": "image/tiff",
    ".pdf": "application/pdf",
}


def _get_ext(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _get_mime(filename: str) -> str:
    ext = f".{_get_ext(filename)}"
    return _MIME_MAP.get(ext, "application/octet-stream")


def _validate_file(filename: str, size: int) -> None:
    ext = f".{_get_ext(filename)}"
    if ext not in ALLOWED_IMAGE_EXT and ext not in ALLOWED_PDF_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {ext}. Allowed: images ({', '.join(sorted(ALLOWED_IMAGE_EXT))}) and PDF.",
        )
    if size > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail=f"File too large ({size} bytes, max {MAX_FILE_SIZE})")


async def _call_bangla_ocr(client: httpx.AsyncClient, data: bytes, filename: str, mime: str | None = None) -> dict[str, Any]:
    """Send a single file (image or pre-converted PNG) to BanglaOCR."""
    content_type = mime or _get_mime(filename)
    files = {"file": (filename, data, content_type)}
    try:
        resp = await client.post(BANGLA_OCR_URL, files=files, headers=OCR_HEADERS, timeout=120.0)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as exc:
        logger.error("BanglaOCR HTTP error for %s: %s %s", filename, exc.response.status_code, exc.response.text[:300])
        raise HTTPException(status_code=502, detail=f"BanglaOCR error: {exc.response.status_code}") from exc
    except httpx.RequestError as exc:
        logger.error("BanglaOCR request error for %s: %s", filename, exc)
        raise HTTPException(status_code=502, detail=f"BanglaOCR connection error: {exc}") from exc


MAX_CONCURRENT_OCR = 32


async def _process_pdf_pages(
    client: httpx.AsyncClient,
    pdf_data: bytes,
    filename: str,
    progress_queue: asyncio.Queue | None = None,
) -> dict[str, Any]:
    """Convert PDF pages to PNG via PyMuPDF, send to BanglaOCR in parallel (up to 32 concurrent)."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="PyMuPDF (fitz) is not installed. Install it with: uv add pymupdf",
        )

    doc = fitz.open(stream=pdf_data, filetype="pdf")
    page_count = len(doc)
    if page_count == 0:
        raise HTTPException(status_code=400, detail="PDF has no pages")

    # Convert all pages to PNG upfront (fast, CPU-bound but trivial)
    page_images: list[tuple[int, bytes]] = []
    for page_num in range(page_count):
        page = doc[page_num]
        pix = page.get_pixmap(dpi=200)
        page_images.append((page_num + 1, pix.tobytes("png")))
    doc.close()

    sem = asyncio.Semaphore(MAX_CONCURRENT_OCR)
    completed = 0
    pages_result: list[dict[str, Any]] = [None] * page_count  # type: ignore[list-item]
    total_duration = 0.0

    async def _ocr_page(page_num: int, img_bytes: bytes) -> None:
        nonlocal completed, total_duration
        async with sem:
            try:
                result = await _call_bangla_ocr(
                    client, img_bytes, f"{filename}_page{page_num}.png", mime="image/png"
                )
                pages_result[page_num - 1] = {
                    "page": page_num,
                    "text": result.get("full_text", ""),
                    "duration_sec": result.get("duration_sec", 0.0),
                }
                total_duration += result.get("duration_sec", 0.0)
            except Exception as exc:
                logger.error("OCR failed for %s page %d: %s", filename, page_num, exc)
                pages_result[page_num - 1] = {"page": page_num, "text": "", "error": str(exc)}
            finally:
                completed += 1
                if progress_queue is not None:
                    await progress_queue.put({"completed": completed, "total": page_count, "source": filename})

    tasks = [_ocr_page(pn, img) for pn, img in page_images]
    await asyncio.gather(*tasks)

    # Build ordered output
    combined_parts: list[str] = []
    final_pages: list[dict[str, Any]] = []
    for pr in pages_result:
        if pr is None:
            continue
        combined_parts.append(f"--- Page {pr['page']} ---\n{pr.get('text', '')}")
        final_pages.append(pr)

    return {
        "job_id": None,
        "kind": "pdf",
        "page_count": page_count,
        "duration_sec": round(total_duration, 2),
        "full_text": "\n\n".join(combined_parts),
        "pages": final_pages,
        "source_filename": filename,
    }


@router.post("/api/ocr/recognize")
async def ocr_recognize(file: UploadFile = File(...)) -> dict[str, Any]:
    """Recognize text from a single uploaded image or PDF file."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")

    data = await file.read()
    _validate_file(file.filename, len(data))

    ext = f".{_get_ext(file.filename)}"

    async with httpx.AsyncClient() as client:
        if ext in ALLOWED_PDF_EXT:
            return await _process_pdf_pages(client, data, file.filename)
        else:
            result = await _call_bangla_ocr(client, data, file.filename)
            result["source_filename"] = file.filename
            return result


async def _process_single_file(
    client: httpx.AsyncClient,
    data: bytes,
    filename: str,
    sem: asyncio.Semaphore,
    progress_queue: asyncio.Queue | None = None,
) -> dict[str, Any]:
    """Process one file (image or PDF) with concurrency control."""
    async with sem:
        ext = f".{_get_ext(filename)}"
        try:
            _validate_file(filename, len(data))
            if ext in ALLOWED_PDF_EXT:
                return await _process_pdf_pages(client, data, filename, progress_queue)
            else:
                result = await _call_bangla_ocr(client, data, filename)
                result["source_filename"] = filename
                if progress_queue is not None:
                    await progress_queue.put({"completed": 1, "total": 1, "source": filename})
                return result
        except HTTPException as exc:
            return {"error": exc.detail, "source_filename": filename}
        except Exception as exc:
            logger.error("Unexpected OCR error for %s: %s", filename, exc)
            return {"error": str(exc), "source_filename": filename}


@router.post("/api/ocr/batch")
async def ocr_batch(files: list[UploadFile] = File(...)) -> list[dict[str, Any]]:
    """Recognize text from multiple uploaded files in parallel (up to 32 concurrent)."""
    if not files:
        raise HTTPException(status_code=400, detail="No files provided")
    if len(files) > 50:
        raise HTTPException(status_code=400, detail="Too many files (max 50)")

    # Read all files upfront
    file_data: list[tuple[str, bytes]] = []
    for file in files:
        if not file.filename:
            continue
        data = await file.read()
        file_data.append((file.filename, data))

    sem = asyncio.Semaphore(MAX_CONCURRENT_OCR)
    async with httpx.AsyncClient() as client:
        tasks = [_process_single_file(client, data, fname, sem) for fname, data in file_data]
        results = await asyncio.gather(*tasks)

    return list(results)


from fastapi.responses import StreamingResponse


@router.post("/api/ocr/stream")
async def ocr_stream(files: list[UploadFile] = File(...)) -> StreamingResponse:
    """SSE endpoint: process files in parallel with real-time progress events."""
    if not files:
        raise HTTPException(status_code=400, detail="No files provided")
    if len(files) > 50:
        raise HTTPException(status_code=400, detail="Too many files (max 50)")

    # Read all files upfront
    file_data: list[tuple[str, bytes]] = []
    for file in files:
        if not file.filename:
            continue
        data = await file.read()
        file_data.append((file.filename, data))

    total_files = len(file_data)
    progress_queue: asyncio.Queue = asyncio.Queue()

    async def _run():
        sem = asyncio.Semaphore(MAX_CONCURRENT_OCR)
        async with httpx.AsyncClient() as client:
            tasks = [
                _process_single_file(client, data, fname, sem, progress_queue)
                for fname, data in file_data
            ]
            results = await asyncio.gather(*tasks)
        # Signal completion
        await progress_queue.put({"done": True, "results": list(results)})

    async def _event_generator():
        import json as _json

        # Start processing in background
        task = asyncio.create_task(_run())
        files_completed = 0

        while not task.done() or not progress_queue.empty():
            try:
                msg = await asyncio.wait_for(progress_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                yield f"data: {_json.dumps({'type': 'heartbeat'})}\n\n"
                continue

            if msg.get("done"):
                yield f"data: {_json.dumps({'type': 'complete', 'results': msg['results']})}\n\n"
                break
            else:
                files_completed += 1
                yield f"data: {_json.dumps({'type': 'progress', 'files_completed': files_completed, 'files_total': total_files, 'pages_completed': msg.get('completed'), 'pages_total': msg.get('total'), 'source': msg.get('source')})}\n\n"

        # Ensure task is done
        if not task.done():
            await task

    return StreamingResponse(_event_generator(), media_type="text/event-stream")
