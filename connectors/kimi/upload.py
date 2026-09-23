"""Kimi file upload — via the ghost session's in-page multipart POST.

Flow (mirrors the UI):
  1. POST /apiv2-files/file/upload  (multipart)  -> {file:{id, meta, blob}}
  2. poll FileService/GetFileParseProgress until PROCESS_STATUS_SUCCESS
  3. reference the returned file id in the Chat message's blocks

Everything runs through the browser (see ghost.JS_UPLOAD) so the SPA's auth +
shield headers apply, exactly like the chat transport.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

logger = logging.getLogger("sable.kimi_upload")

PARSE_PROGRESS_PATH = "/apiv2-files/kimi.gateway.file.v1.FileService/GetFileParseProgress"

_MIME_MAP: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".py": "text/x-python",
    ".js": "text/javascript",
    ".ts": "text/plain",
    ".html": "text/html",
    ".xml": "application/xml",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def _mime_for(name: str) -> str:
    return _MIME_MAP.get(os.path.splitext(name)[1].lower(), "application/octet-stream")


async def upload_file(session, file_path: str, *, poll: bool = True,
                      timeout: float = 30.0) -> dict[str, Any]:
    """Upload one file through the ghost session and wait for parse success.

    ``session`` is a ``KimiGhostSession``. Returns a dict with ``file_id``,
    ``status``, ``file_name``, ``file_size``, ``is_image`` and raw metadata.
    """
    abs_path = os.path.abspath(os.path.expanduser(file_path))
    if not os.path.exists(abs_path):
        raise FileNotFoundError(f"Upload file not found: {abs_path}")

    file_name = os.path.basename(abs_path)
    file_size = os.path.getsize(abs_path)
    mime = _mime_for(file_name)
    with open(abs_path, "rb") as fh:
        data = fh.read()

    resp = await session.upload_file(file_name, mime, data)
    fobj = (resp or {}).get("file") or {}
    file_id = fobj.get("id")
    if not file_id:
        raise RuntimeError(f"Kimi upload response missing file id: {resp}")

    meta = fobj.get("meta") or {}
    status = "PROCESS_STATUS_SUCCESS"

    if poll:
        status = await _poll_parse(session, str(file_id), timeout=timeout)

    logger.info("Kimi upload OK: %s -> %s (%s)", file_name, file_id, status)
    return {
        "file_id": str(file_id),
        "status": status,
        "file_name": meta.get("name") or file_name,
        "file_size": int(meta.get("sizeBytes") or file_size),
        "is_image": (meta.get("type") == "FILE_TYPE_IMAGE"),
        "raw": fobj,
    }


async def _poll_parse(session, file_id: str, *, timeout: float = 30.0) -> str:
    """Poll GetFileParseProgress until the file is parsed (or timeout)."""
    deadline = asyncio.get_event_loop().time() + timeout
    last = "PROCESS_STATUS_PENDING"
    while asyncio.get_event_loop().time() < deadline:
        try:
            frames = await session.connect_json(PARSE_PROGRESS_PATH, {"file_id": file_id})
        except Exception as exc:
            logger.debug("Kimi parse poll error: %s", exc)
            frames = []
        for fr in frames:
            f = fr.get("file") if isinstance(fr, dict) else None
            if isinstance(f, dict) and f.get("status"):
                last = f["status"]
            status = fr.get("status") if isinstance(fr, dict) else None
            if status:
                last = status
        if last in ("PROCESS_STATUS_SUCCESS", "PROCESS_STATUS_FAILED"):
            return last
        await asyncio.sleep(1.0)
    return last


async def upload_files(session, paths: list[str], *, poll: bool = True) -> list[dict[str, Any]]:
    """Upload several files sequentially (serialized on one ghost session)."""
    out: list[dict[str, Any]] = []
    for p in paths:
        out.append(await upload_file(session, p, poll=poll))
    return out
