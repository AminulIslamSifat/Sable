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


# NOTE: there was previously a `_convert_with_pypdf` fallback here. It was
# removed deliberately — pypdf cannot rasterise a PDF page, so the function
# only ever produced blank white images at the correct dimensions and reported
# success. That made a missing Poppler look like an empty document, and an
# agent reading those images would author coordinates against nothing. Callers
# now get a RuntimeError naming the package to install.


# Converts each page of a PDF to a PNG image.


def convert(pdf_path: str, output_dir: str, max_dim: int = 1000) -> None:
    """Convert every page of *pdf_path* to a PNG inside *output_dir*.

    Requires Poppler (pdf2image). There is deliberately no silent fallback:
    pypdf and Pillow cannot rasterise a PDF page, and the previous "fallback"
    only produced blank white images at the correct dimensions. That is worse
    than failing — the caller (an agent) would read empty pages, conclude the
    document was blank, and author coordinates against nothing, with exit code
    0 the whole way. A missing Poppler is now a loud, actionable error.
    """
    if not _HAS_POPPLER:
        raise RuntimeError(
            "PDF rendering needs Poppler (pdftoppm), which is not on PATH. "
            "Install it and retry — e.g. `pacman -S poppler` (Arch), "
            "`apt install poppler-utils` (Debian/Ubuntu), "
            "`brew install poppler` (macOS). "
            "No pure-Python substitute exists: pypdf cannot rasterise pages."
        )

    print(f"[pdf] Using Poppler backend (pdf2image)")
    images = _convert_with_poppler(pdf_path, dpi=200)

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
