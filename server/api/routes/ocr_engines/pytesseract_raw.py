"""Pytesseract — Raw tesseract OCR with no preprocessing or normalization.

Simplest possible pipeline: decode → image_to_string → done.
Useful as a baseline or when you want unprocessed output.
"""
from __future__ import annotations

from typing import Any

import cv2
import numpy as np
import pytesseract as pts


def run(image_bytes: bytes, filename: str, lang: str) -> dict[str, Any]:
    """Run raw pytesseract on image bytes."""
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
