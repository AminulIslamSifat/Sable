"""OCR engine registry — each provider lives in its own module."""
from __future__ import annotations

from typing import Any, Callable

# Each engine module exposes: run(image_bytes, filename, lang) -> dict
# Lazy imports so missing deps don't break the whole package.

_ENGINE_RUNNERS: dict[str, str] = {
    "sableocr": ".sableocr",
    "paddleocr": ".paddle_engine",
    "pytesseract": ".pytesseract_raw",
}


def get_runner(provider_id: str) -> Callable[[bytes, str, str], dict[str, Any]]:
    """Return the run() callable for a provider. Raises ImportError if deps missing."""
    if provider_id not in _ENGINE_RUNNERS:
        raise ValueError(f"Unknown OCR engine: {provider_id}")

    from importlib import import_module
    mod = import_module(_ENGINE_RUNNERS[provider_id], package=__name__)
    return mod.run
