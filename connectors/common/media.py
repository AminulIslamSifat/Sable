
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


# ---------------------------------------------------------------------------
# Document → Image conversion (for models that support image but not document)
# ---------------------------------------------------------------------------

# Extensions worth converting to page images
_CONVERTIBLE_EXTENSIONS = frozenset({".pdf", ".pptx", ".ppt"})
_MAX_CONVERT_PAGES = 20


def is_convertible_doc(path: str | Path) -> bool:
    """Check if a file is a document type we can convert to page images."""
    return Path(path).suffix.lower() in _CONVERTIBLE_EXTENSIONS


def convert_doc_to_images(
    doc_path: str,
    *,
    max_pages: int = _MAX_CONVERT_PAGES,
    dpi: int = 200,
) -> list[str] | None:
    """Convert a document (PDF/PPTX) to per-page PNG images.

    Returns list of image file paths on success, None if conversion fails
    or document exceeds max_pages.
    """
    src = Path(doc_path)
    suffix = src.suffix.lower()

    if suffix not in _CONVERTIBLE_EXTENSIONS:
        return None

    try:
        if suffix == ".pdf":
            return _convert_pdf_to_images(src, max_pages=max_pages, dpi=dpi)
        elif suffix in (".pptx", ".ppt"):
            return _convert_pptx_to_images(src, max_pages=max_pages)
    except Exception as exc:
        logger.warning("Doc→image conversion failed for %s: %s", doc_path, exc)
        return None

    return None


def _convert_pdf_to_images(
    pdf_path: Path,
    *,
    max_pages: int,
    dpi: int,
) -> list[str] | None:
    """Convert PDF pages to PNG images using PyMuPDF."""
    import tempfile
    import pymupdf as fitz

    doc = fitz.open(str(pdf_path))
    num_pages = len(doc)

    if num_pages > max_pages:
        logger.warning(
            "PDF has %d pages (max %d), skipping conversion: %s",
            num_pages, max_pages, pdf_path,
        )
        doc.close()
        return None

    out_dir = Path(tempfile.mkdtemp(prefix="sable_doc2img_"))
    images: list[str] = []

    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)

    for i in range(num_pages):
        page = doc.load_page(i)
        pix = page.get_pixmap(matrix=mat)
        img_path = out_dir / f"page_{i + 1:04d}.png"
        pix.save(str(img_path))
        images.append(str(img_path))

    doc.close()
    logger.info("Converted %d-page PDF to images: %s", num_pages, pdf_path)
    return images


def _convert_pptx_to_images(
    pptx_path: Path,
    *,
    max_pages: int,
) -> list[str] | None:
    """Convert PPTX slides to PNG images via LibreOffice headless.

    Falls back to None if LibreOffice is not installed (python-pptx cannot
    render slides natively).
    """
    import subprocess
    import shutil
    import tempfile

    if not shutil.which("libreoffice") and not shutil.which("soffice"):
        logger.warning(
            "LibreOffice not found — cannot convert PPTX to images: %s", pptx_path,
        )
        return None

    # Quick slide count check via python-pptx (no rendering needed)
    try:
        from pptx import Presentation
        prs = Presentation(str(pptx_path))
        num_slides = len(prs.slides)
        if num_slides > max_pages:
            logger.warning(
                "PPTX has %d slides (max %d), skipping conversion: %s",
                num_slides, max_pages, pptx_path,
            )
            return None
    except Exception:
        pass  # Proceed anyway, LibreOffice will handle it

    out_dir = Path(tempfile.mkdtemp(prefix="sable_doc2img_"))
    lo_bin = shutil.which("libreoffice") or shutil.which("soffice")

    try:
        subprocess.run(
            [
                lo_bin, "--headless", "--norestore", "--convert-to", "png",
                "--outdir", str(out_dir), str(pptx_path),
            ],
            capture_output=True,
            timeout=120,
            check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.warning("LibreOffice PPTX→PNG failed for %s: %s", pptx_path, exc)
        return None

    # LibreOffice outputs one PNG per slide named <basename>-N.png or <basename>.png
    pngs = sorted(out_dir.glob("*.png"))
    if not pngs:
        logger.warning("LibreOffice produced no PNGs for: %s", pptx_path)
        return None

    # Rename to consistent page_NNNN.png format
    images: list[str] = []
    for i, png in enumerate(pngs):
        dest = out_dir / f"slide_{i + 1:04d}.png"
        png.rename(dest)
        images.append(str(dest))

    logger.info("Converted %d-slide PPTX to images: %s", len(images), pptx_path)
    return images

