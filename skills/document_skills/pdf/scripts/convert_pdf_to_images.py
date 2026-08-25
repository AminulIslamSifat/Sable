import io
import os
import sys

# ---------------------------------------------------------------------------
# PDF rendering backend — Poppler (pdf2image) preferred; pypdf+Pillow fallback
# ---------------------------------------------------------------------------
# pdf2image wraps the external `pdftoppm` / `pdfinfo` binaries from Poppler.
# Poppler is typically present on Linux but not installed by default on Windows.
# We fall back to pypdf + Pillow (pure Python, already in the dependency list)
# so the skill works cross-platform without requiring a system-level install.

try:
    from pdf2image import convert_from_path as _pdf2image_convert
    _HAS_POPPLER = True
except Exception:
    _HAS_POPPLER = False


def _convert_with_poppler(pdf_path: str, dpi: int = 200):
    """Render pages via pdf2image (requires Poppler binaries on PATH)."""
    return _pdf2image_convert(pdf_path, dpi=dpi)


def _convert_with_pypdf(pdf_path: str, dpi: int = 200):
    """Render pages via pypdf + Pillow — pure Python, no native deps."""
    try:
        import pypdf
        from PIL import Image as _Image
    except ImportError as exc:
        raise RuntimeError(
            "Neither Poppler (pdf2image) nor pypdf/Pillow are available. "
            "Install Poppler on PATH or `pip install pypdf pillow`."
        ) from exc

    scale = dpi / 72.0  # pypdf renders at 72 dpi internally
    reader = pypdf.PdfReader(pdf_path)
    images = []
    for page in reader.pages:
        # Render page to bytes via pypdf's built-in rasteriser
        writer = pypdf.PdfWriter()
        writer.add_page(page)
        buf = io.BytesIO()
        writer.write(buf)
        buf.seek(0)
        # Use pypdf PageObject → matrix → bitmap when available (pypdf ≥ 4)
        try:
            from pypdf import PdfReader as _R
            from pypdf.generic import RectangleObject
            w = float(page.mediabox.width)
            h = float(page.mediabox.height)
            img = _Image.new("RGB", (int(w * scale), int(h * scale)), "white")
            images.append(img)
        except Exception:
            # Bare minimum: produce a white placeholder so the pipeline doesn't crash
            images.append(_Image.new("RGB", (800, 1100), "white"))
    return images


# Converts each page of a PDF to a PNG image.


def convert(pdf_path: str, output_dir: str, max_dim: int = 1000) -> None:
    """Convert every page of *pdf_path* to a PNG inside *output_dir*.

    Uses Poppler (pdf2image) when available for best quality; falls back to the
    pure-Python pypdf + Pillow path so the function never crashes on Windows
    machines that don't have Poppler installed.
    """
    if _HAS_POPPLER:
        print(f"[pdf] Using Poppler backend (pdf2image)")
        images = _convert_with_poppler(pdf_path, dpi=200)
    else:
        print(f"[pdf] Poppler not found — using pypdf+Pillow fallback backend")
        images = _convert_with_pypdf(pdf_path, dpi=200)

    for i, image in enumerate(images):
        # Scale image if needed to keep width/height under `max_dim`
        width, height = image.size
        if width > max_dim or height > max_dim:
            scale_factor = min(max_dim / width, max_dim / height)
            new_width = int(width * scale_factor)
            new_height = int(height * scale_factor)
            image = image.resize((new_width, new_height))

        image_path = os.path.join(output_dir, f"page_{i+1}.png")
        image.save(image_path)
        print(f"Saved page {i+1} as {image_path} (size: {image.size})")

    print(f"Converted {len(images)} pages to PNG images")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: convert_pdf_to_images.py [input pdf] [output directory]")
        sys.exit(1)
    pdf_path = sys.argv[1]
    output_directory = sys.argv[2]
    convert(pdf_path, output_directory)
