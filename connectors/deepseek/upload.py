
"""DeepSeek file upload via pure httpx + PoW. No browser required.

Flow: request PoW challenge → solve nonce (Go binary, ~84ms) → multipart POST.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

UPLOAD_TARGET_PATH = "/api/v0/file/upload_file"

# Guess content-type from extension
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
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


async def upload_file(
    file_path: str,
    model_type: str = "vision",
    thinking_enabled: bool = False,
) -> dict[str, Any]:
    """Upload a file to DeepSeek via httpx multipart POST with PoW.

    Args:
        file_path: Local path of the file to upload.
        model_type: DeepSeek model type header, default ``vision``.
        thinking_enabled: Whether thinking mode is active.

    Returns:
        Dict with ``file_id``, ``status``, ``file_name``, ``file_size``,
        ``model_kind``, ``is_image``, and ``raw`` metadata.
    """
    from connectors.deepseek.client import get_client

    abs_path = os.path.abspath(os.path.expanduser(file_path))
    if not os.path.exists(abs_path):
        raise FileNotFoundError(f"Upload file not found: {abs_path}")

    file_size = os.path.getsize(abs_path)
    file_name = os.path.basename(abs_path)
    ext = os.path.splitext(file_name)[1].lower()
    mime = _MIME_MAP.get(ext, "application/octet-stream")

    client = get_client()

    # Step 1: Get PoW challenge for upload endpoint
    http = await client._get_http()
    resp = await http.post(
        "/api/v0/chat/create_pow_challenge",
        json={"target_path": UPLOAD_TARGET_PATH},
        headers=client._auth_headers(),
    )
    if resp.status_code == 401:
        await client._refresh_token()
        resp = await http.post(
            "/api/v0/chat/create_pow_challenge",
            json={"target_path": UPLOAD_TARGET_PATH},
            headers=client._auth_headers(),
        )
    resp.raise_for_status()

    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Challenge failed: {data.get('msg', 'unknown')}")
    challenge = data["data"]["biz_data"]["challenge"]

    # Step 2: Solve PoW in thread pool
    loop = asyncio.get_event_loop()
    nonce = await loop.run_in_executor(None, client._solve_pow, challenge)

    # Step 3: Build header and upload
    pow_header = client._build_pow_header(challenge, nonce)

    headers = {
        **client._auth_headers(),
        "x-ds-pow-response": pow_header,
        "x-model-type": model_type,
        "x-thinking-enabled": "1" if thinking_enabled else "0",
        "x-file-size": str(file_size),
    }

    with open(abs_path, "rb") as f:
        file_bytes = f.read()

    upload_resp = await http.post(
        UPLOAD_TARGET_PATH,
        headers=headers,
        files={"file": (file_name, file_bytes, mime)},
    )
    upload_resp.raise_for_status()

    payload = upload_resp.json()
    if payload.get("code") != 0:
        raise RuntimeError(f"DeepSeek upload failed: {payload}")

    biz = payload.get("data", {}).get("biz_data", {})
    file_id = biz.get("id")
    if not file_id:
        raise RuntimeError(f"DeepSeek upload response missing file id: {payload}")

    logger.info("DeepSeek upload OK: %s → %s", file_name, file_id)

    return {
        "file_id": str(file_id),
        "status": biz.get("status"),
        "file_name": biz.get("file_name") or file_name,
        "file_size": biz.get("file_size") or file_size,
        "model_kind": biz.get("model_kind"),
        "is_image": bool(biz.get("is_image", False)),
        "raw": biz,
    }


# Backward compat alias — old callers pass browser_manager as first arg
async def upload_file_via_browser_manager(
    browser_manager: Any,
    file_path: str,
    model_type: str = "vision",
    thinking_enabled: bool = False,
) -> dict[str, Any]:
    """Legacy wrapper. Ignores browser_manager, uses pure httpx."""
    return await upload_file(file_path, model_type=model_type, thinking_enabled=thinking_enabled)
