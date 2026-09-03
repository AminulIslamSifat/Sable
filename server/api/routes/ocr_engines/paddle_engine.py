"""PaddleOCR — Local OCR using PaddlePaddle inference.

Adapted from wos/core/ocr.py for Sable's pipeline.
Uses CPU-only mode with aggressive memory flags to avoid bloat.
"""
from __future__ import annotations

import os
from typing import Any

# Must be set BEFORE importing paddle/paddleocr
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
os.environ["FLAGS_eager_delete_tensor_gb"] = "0.0"
os.environ["FLAGS_fast_eager_deletion_mode"] = "True"
os.environ["FLAGS_allocator_strategy"] = "naive_best_fit"
os.environ.setdefault("ONEDNN_PRIMITIVE_CACHE_CAPACITY", "10")

import cv2
import numpy as np
from paddleocr import PaddleOCR


def _build_engine(lang: str) -> PaddleOCR:
    """Create a PaddleOCR instance with Sable-optimized settings."""
    return PaddleOCR(
        use_angle_cls=False,
        lang=lang,
        use_gpu=False,
        det_limit_side_len=1024,
        cpu_threads=min((os.cpu_count() or 1), 4),
        ir_optim=True,
        layout=False,
        table=False,
        formula=False,
    )


def run(image_bytes: bytes, filename: str, lang: str) -> dict[str, Any]:
    """Run PaddleOCR on raw image bytes."""
    buf = np.frombuffer(image_bytes, np.uint8)
    img_cv = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if img_cv is None:
        raise ValueError(f"Could not decode image: {filename}")

    ocr = _build_engine(lang)
    output = ocr.ocr(img_cv, cls=False)

    texts: list[str] = []
    scores: list[float] = []
    if output and output[0]:
        for line in output[0]:
            if not line or not isinstance(line, list) or len(line) < 2:
                continue
            text = line[1][0]
            score = float(line[1][1])
            if score > 0.5:
                texts.append(text)
                scores.append(score)

    return {
        "full_text": "\n".join(texts),
        "line_count": len(texts),
        "avg_confidence": round(sum(scores) / len(scores), 3) if scores else 0.0,
        "source_filename": filename,
        "engine": "paddleocr",
    }
