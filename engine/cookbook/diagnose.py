
"""Output diagnosis — parse llama-server logs for common errors."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Diagnosis:
    pattern: str
    message: str
    suggestions: list[str]


_PATTERNS: list[Diagnosis] = [
    Diagnosis(
        pattern=r"failed to mmap|Cannot allocate memory|mmap failed",
        message="Not enough RAM to load the model.",
        suggestions=[
            "Try a smaller quantization (Q3_K_M or Q2_K)",
            "Close other applications to free RAM",
            "Use a smaller model (3B instead of 7B)",
        ],
    ),
    Diagnosis(
        pattern=r"address already in use|bind: Address already in use",
        message="Port is already in use by another process.",
        suggestions=[
            "Choose a different port",
            "Stop the other process using this port",
        ],
    ),
    Diagnosis(
        pattern=r"failed to open model|error loading model|llama_model_load.*failed",
        message="Model file is corrupted or incompatible.",
        suggestions=[
            "Re-download the model file",
            "Verify the file is a valid GGUF",
            "Check llama.cpp version compatibility",
        ],
    ),
    Diagnosis(
        pattern=r"context.*too (small|large)|n_ctx.*exceeds",
        message="Context size is incompatible with model limits.",
        suggestions=[
            "Reduce --ctx-size to 2048 or 4096",
            "Check the model's maximum context length",
        ],
    ),
    Diagnosis(
        pattern=r"segfault|SIGSEGV|segmentation fault",
        message="llama-server crashed (segfault).",
        suggestions=[
            "Update llama.cpp: sudo pacman -Syu llama-cpp",
            "Try with fewer threads (--threads 2)",
            "The model file may be corrupted — re-download",
        ],
    ),
    Diagnosis(
        pattern=r"unknown model|unsupported.*arch",
        message="Model architecture not supported by this llama.cpp version.",
        suggestions=[
            "Update llama.cpp to latest version",
            "Check if the model requires a specific llama.cpp build",
        ],
    ),
]


def diagnose_output(text: str) -> dict | None:
    """Analyze server output for known error patterns.
    
    Returns structured diagnosis or None if no known pattern matched.
    """
    if not text:
        return None

    # Check last 4000 chars for errors
    tail = text[-4000:]

    for diag in _PATTERNS:
        if re.search(diag.pattern, tail, re.IGNORECASE):
            return {
                "message": diag.message,
                "suggestions": diag.suggestions,
            }

    # Generic error detection
    if "error" in tail.lower() or "fatal" in tail.lower():
        return {
            "message": "Unknown error detected in server output.",
            "suggestions": ["Check the full log output for details"],
        }

    return None
