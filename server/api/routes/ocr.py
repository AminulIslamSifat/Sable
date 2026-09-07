"""OCR route — proxies to BanglaOCR API with parallel PDF page splitting."""
from __future__ import annotations

import asyncio
import io
import logging
import random
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

# Free proxy sources — fetched on demand, cached briefly
_PROXY_CACHE: dict[str, Any] = {"proxies": [], "fetched_at": 0.0}
_PROXY_CACHE_TTL = 300  # refresh every 5 min
_PROXY_SOURCES = [
    "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&protocol=http&proxy_format=protocolipport&format=text&timeout=5000",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
]


async def _fetch_proxies(client: httpx.AsyncClient) -> list[str]:
    """Fetch fresh proxy list from free sources."""
    import time
    now = time.time()
    if _PROXY_CACHE["proxies"] and (now - _PROXY_CACHE["fetched_at"]) < _PROXY_CACHE_TTL:
        return _PROXY_CACHE["proxies"]

    proxies: list[str] = []
    for url in _PROXY_SOURCES:
        try:
            resp = await client.get(url, timeout=10.0)
            if resp.status_code == 200:
                for line in resp.text.strip().splitlines():
                    line = line.strip()
                    if line and ":" in line:
                        # Normalize: strip protocol prefix if present
                        proxy = line.split("//")[-1] if "://" in line else line
                        if not proxy.startswith("http"):
                            proxy = f"http://{proxy}"
                        proxies.append(proxy)
                if proxies:
                    break  # got enough from first source
        except Exception:
            continue

    if proxies:
        _PROXY_CACHE["proxies"] = proxies[:100]  # cap at 100
        _PROXY_CACHE["fetched_at"] = now
        logger.info("Fetched %d proxies for OCR rotation", len(_PROXY_CACHE["proxies"]))
    return _PROXY_CACHE["proxies"]
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


PER_PROXY_TIMEOUT = 15.0   # seconds per proxy attempt (fast fail on dead proxies)
DIRECT_TIMEOUT = 30.0       # slightly longer for direct connection
OVERALL_OCR_TIMEOUT = 300.0  # 5 min hard cap per file total


async def _call_bangla_ocr(client: httpx.AsyncClient, data: bytes, filename: str, mime: str | None = None) -> dict[str, Any]:
    """Send a single file to BanglaOCR with proxy rotation on 429 and strict timeouts."""
    content_type = mime or _get_mime(filename)
    files = {"file": (filename, data, content_type)}

    deadline = asyncio.get_event_loop().time() + OVERALL_OCR_TIMEOUT

    # Try direct first, then rotate proxies on 429
    proxies: list[str | None] = [None]  # None = direct connection
    try:
        proxy_list = await asyncio.wait_for(_fetch_proxies(client), timeout=8.0)
        if proxy_list:
            proxies.extend(random.sample(proxy_list, min(10, len(proxy_list))))
    except Exception:
        pass

    last_exc: Exception | None = None
    for attempt, proxy in enumerate(proxies):
        # Check overall deadline before each attempt
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            logger.warning("BanglaOCR overall timeout for %s after %d attempts", filename, attempt)
            break

        timeout = DIRECT_TIMEOUT if proxy is None else min(PER_PROXY_TIMEOUT, remaining)
        req_client: httpx.AsyncClient | None = None
        try:
            if proxy:
                req_client = httpx.AsyncClient(proxy=proxy, timeout=timeout)
                resp = await req_client.post(BANGLA_OCR_URL, files=files, headers=OCR_HEADERS)
            else:
                resp = await client.post(BANGLA_OCR_URL, files=files, headers=OCR_HEADERS, timeout=timeout)

            if resp.status_code == 429:
                logger.warning("BanglaOCR 429 for %s via %s (%d/%d), rotating",
                               filename, proxy or "direct", attempt + 1, len(proxies))
                continue

            resp.raise_for_status()
            return resp.json()

        except httpx.HTTPStatusError as exc:
            last_exc = exc
            if exc.response.status_code == 429:
                logger.warning("BanglaOCR 429 for %s via %s (%d/%d), rotating",
                               filename, proxy or "direct", attempt + 1, len(proxies))
                continue
            logger.error("BanglaOCR HTTP error for %s: %s", filename, exc.response.status_code)
            raise HTTPException(status_code=502, detail=f"BanglaOCR error: {exc.response.status_code}") from exc

        except (httpx.TimeoutException, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
            last_exc = exc
            logger.warning("BanglaOCR timeout for %s via %s (%.1fs): %s",
                           filename, proxy or "direct", timeout, exc)
            continue

        except (httpx.RequestError, httpx.ProxyError) as exc:
            last_exc = exc
            logger.warning("BanglaOCR proxy error for %s via %s: %s", filename, proxy or "direct", exc)
            continue

        except Exception as exc:
            last_exc = exc
            logger.warning("BanglaOCR unexpected error for %s via %s: %s", filename, proxy or "direct", exc)
            continue

        finally:
            if req_client is not None:
                try:
                    await req_client.aclose()
                except Exception:
                    pass

    raise HTTPException(
        status_code=502,
        detail=f"BanglaOCR failed after {len(proxies)} attempts ({OVERALL_OCR_TIMEOUT}s cap): {last_exc}"
    )


MAX_CONCURRENT_OCR = 4
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0  # seconds


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
            except HTTPException as exc:
                logger.error("OCR HTTP error for %s page %d: %s", filename, page_num, exc.detail)
                pages_result[page_num - 1] = {"page": page_num, "text": f"[Error: {exc.detail}]", "error": exc.detail}
            except Exception as exc:
                logger.error("OCR failed for %s page %d: %s", filename, page_num, exc)
                pages_result[page_num - 1] = {"page": page_num, "text": f"[Error: {exc}]", "error": str(exc)}
            finally:
                completed += 1
                if progress_queue is not None:
                    await progress_queue.put({"completed": 1})

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

    # Pre-scan PDFs to get total page count upfront
    total_pages = 0
    for fname, data in file_data:
        ext = f".{_get_ext(fname)}"
        if ext in ALLOWED_PDF_EXT:
            try:
                import fitz
                doc = fitz.open(stream=data, filetype="pdf")
                total_pages += len(doc)
                doc.close()
            except Exception:
                total_pages += 1  # fallback
        else:
            total_pages += 1  # images count as 1 page

    async def _run():
        sem = asyncio.Semaphore(MAX_CONCURRENT_OCR)
        async with httpx.AsyncClient() as client:
            tasks = [
                _process_single_file(client, data, fname, sem, progress_queue)
                for fname, data in file_data
            ]
            results = await asyncio.gather(*tasks)
        await progress_queue.put({"done": True, "results": list(results)})

    async def _event_generator():
        import json as _json

        task = asyncio.create_task(_run())
        pages_done = 0

        # Emit initial total so frontend knows the scale
        yield f"data: {_json.dumps({'type': 'init', 'total_pages': total_pages, 'total_files': total_files})}\n\n"

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
                pages_done += msg.get("completed", 1)
                pct = round((pages_done / total_pages) * 100) if total_pages else 0
                yield f"data: {_json.dumps({'type': 'progress', 'pages_done': pages_done, 'total_pages': total_pages, 'pct': pct})}\n\n"

        if not task.done():
            await task

    return StreamingResponse(_event_generator(), media_type="text/event-stream")
