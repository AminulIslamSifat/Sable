"""SableOCR — Bangla+English OCR with dual-pass preprocessing and Unicode normalization.

Rewritten from doc2text/engine/ocr.py to fit Sable's async pipeline.
Pipeline: upscale 3x → grayscale + adaptive threshold → dual tesseract pass →
          merge by confidence → bnunicode normalize → line assembly.
"""
from __future__ import annotations

from typing import Any

import cv2
import numpy as np
import pytesseract as pts
from bnunicodenormalizer import Normalizer

_normalizer = Normalizer()


def _preprocess(img_cv: np.ndarray, scale: int = 3) -> tuple[np.ndarray, np.ndarray, int]:
    """Upscale, grayscale, denoise. Returns (grayscale_denoised, adaptive_binary, scale)."""
    img_up = cv2.resize(img_cv, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(img_up, cv2.COLOR_BGR2GRAY)
    denoised = cv2.fastNlMeansDenoising(gray, None, h=10, templateWindowSize=7, searchWindowSize=21)
    adaptive = cv2.adaptiveThreshold(
        denoised, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 51, 10
    )
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    adaptive = cv2.morphologyEx(adaptive, cv2.MORPH_CLOSE, kernel)
    return denoised, adaptive, scale


def _extract_words(image: np.ndarray, lang: str, scale: int,
                   min_conf: int = 25, min_h: int = 18) -> list[dict]:
    """Run tesseract on a preprocessed image and return filtered word list."""
    data = pts.image_to_data(image, lang=lang, output_type=pts.Output.DICT, config="--psm 6")
    words = []
    for i in range(len(data["text"])):
        text = data["text"][i].strip()
        if not text:
            continue
        conf = data["conf"][i]
        h = data["height"][i]
        if conf < min_conf or h < min_h:
            continue
        r = _normalizer(text)
        normalized = (r["normalized"] if r else None) or text
        words.append({
            "text": normalized,
            "left": data["left"][i] // scale,
            "top": data["top"][i] // scale,
            "width": data["width"][i] // scale,
            "height": h // scale,
            "conf": conf,
        })
    return words


def _merge_words(words_a: list[dict], words_b: list[dict],
                 overlap_thresh: int = 8) -> list[dict]:
    """Merge two word lists, preferring higher confidence at overlapping positions."""
    merged = list(words_a)
    for wb in words_b:
        dominated = False
        for wa in merged:
            y_close = abs(wa["top"] - wb["top"]) <= overlap_thresh
            x_overlap = not (
                wa["left"] + wa["width"] < wb["left"]
                or wb["left"] + wb["width"] < wa["left"]
            )
            if y_close and x_overlap:
                if wb["conf"] > wa["conf"]:
                    wa.update(wb)
                dominated = True
                break
        if not dominated:
            merged.append(wb)
    return merged


def _assemble_lines(words: list[dict], line_gap: int = 15) -> str:
    """Sort words by position and group into text lines."""
    words.sort(key=lambda w: (w["top"], w["left"]))
    lines: list[list[dict]] = []
    current_line: list[dict] = []
    last_top = -999
    for w in words:
        if abs(w["top"] - last_top) > line_gap:
            if current_line:
                lines.append(current_line)
            current_line = [w]
        else:
            current_line.append(w)
        last_top = w["top"]
    if current_line:
        lines.append(current_line)
    return "\n".join(" ".join(w["text"] for w in line) for line in lines)


def run(image_bytes: bytes, filename: str, lang: str) -> dict[str, Any]:
    """Full SableOCR pipeline: preprocess → dual OCR → merge → normalize → assemble."""
    buf = np.frombuffer(image_bytes, np.uint8)
    img_cv = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if img_cv is None:
        raise ValueError(f"Could not decode image: {filename}")

    gray_denoised, adaptive_bin, scale = _preprocess(img_cv)

    # Dual OCR: grayscale (primary) + adaptive binary (secondary)
    words_gray = _extract_words(gray_denoised, lang, scale)
    words_adapt = _extract_words(adaptive_bin, lang, scale)

    # Merge — grayscale is primary, adaptive fills gaps
    merged = _merge_words(words_gray, words_adapt)

    full_text = _assemble_lines(merged)

    return {
        "full_text": full_text,
        "word_count": len(merged),
        "source_filename": filename,
        "engine": "sableocr",
    }
