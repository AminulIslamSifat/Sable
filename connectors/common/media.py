
"""Shared media file utilities for LLM connectors.

Provider-agnostic file reading, MIME detection, base64 encoding, and size validation.
Each connector wraps the output in their own API-specific format.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("sable.media")

# MIME types that Python's mimetypes module may not recognize
_EXTRA_MIMES: dict[str, str] = {
    # Images
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".webp": "image/webp",
    # Video
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".flv": "video/x-flv",
    ".wmv": "video/x-ms-wmv",
    ".3gp": "video/3gpp",
    # Audio
    ".aiff": "audio/aiff",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
}

# Default per-category inline size limits (bytes)
DEFAULT_LIMITS: dict[str, int] = {
    "video": 100 * 1024 * 1024,   # 100 MB
    "image": 20 * 1024 * 1024,    # 20 MB
    "audio": 20 * 1024 * 1024,    # 20 MB
    "document": 20 * 1024 * 1024, # 20 MB
    "other": 20 * 1024 * 1024,    # 20 MB
}


@dataclass(frozen=True)
class PreparedFile:
    """Provider-agnostic prepared file ready for API attachment."""
    mime_type: str
    data_b64: str
    size_bytes: int
    category: str  # "image", "video", "audio", "document", "other"
    path: str


def detect_mime(path: Path) -> str:
    """Detect MIME type with fallback for uncommon formats."""
    mime, _ = mimetypes.guess_type(str(path))
    if mime:
        return mime
    suffix = path.suffix.lower()
    return _EXTRA_MIMES.get(suffix, "application/octet-stream")


def categorize(mime: str) -> str:
    """Map MIME type to a media category."""
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("application/pdf") or mime.startswith("text/"):
        return "document"
    return "other"


def prepare_inline_file(
    fpath: str,
    *,
    max_bytes: int | None = None,
    limits: dict[str, int] | None = None,
) -> PreparedFile | None:
    """Read a local file, validate size, and return base64-encoded result.

    Args:
        fpath: Absolute path to the local file.
        max_bytes: Hard override for max file size (ignores category limits).
        limits: Per-category size limits. Defaults to DEFAULT_LIMITS.

    Returns:
        PreparedFile on success, None if file missing or too large.
    """
    path = Path(fpath)
    if not path.exists():
        logger.warning("File not found: %s", fpath)
        return None

    mime = detect_mime(path)
    cat = categorize(mime)

    raw = path.read_bytes()
    effective_limits = limits or DEFAULT_LIMITS
    limit = max_bytes if max_bytes is not None else effective_limits.get(cat, effective_limits["other"])

    if len(raw) > limit:
        logger.warning(
            "File too large for inline (%d bytes, limit %d, category %s): %s",
            len(raw), limit, cat, fpath,
        )
        return None

    b64 = base64.b64encode(raw).decode("ascii")
    return PreparedFile(
        mime_type=mime,
        data_b64=b64,
        size_bytes=len(raw),
        category=cat,
        path=fpath,
    )


# ---------------------------------------------------------------------------
# Provider-specific formatters — wrap PreparedFile into API-native structures
# ---------------------------------------------------------------------------

def to_gemini_inline(pf: PreparedFile) -> dict[str, Any]:
    """Format as Gemini inlineData part."""
    return {"inlineData": {"mimeType": pf.mime_type, "data": pf.data_b64}}


def to_openai_image(pf: PreparedFile) -> dict[str, Any]:
    """Format as OpenAI image_url content block (images only)."""
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{pf.mime_type};base64,{pf.data_b64}"},
    }


def to_anthropic_source(pf: PreparedFile) -> dict[str, Any]:
    """Format as Anthropic source content block."""
    return {
        "type": pf.category if pf.category in ("image", "document") else "image",
        "source": {"type": "base64", "media_type": pf.mime_type, "data": pf.data_b64},
    }

