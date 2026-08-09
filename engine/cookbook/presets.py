
"""Model presets and hardware-aware recommendations."""

from __future__ import annotations

from dataclasses import dataclass

from .hardware import detect_hardware, get_recommendations, HardwareInfo

# Cache hardware info (it doesn't change during a session)
_cached_hw: HardwareInfo | None = None


@dataclass
class ServePreset:
    id: str
    label: str
    description: str
    repo_id: str
    include: str  # glob pattern for file selection
    ctx_size: int = 4096
    threads: int = 0
    gpu_layers: int = 0
    extra_args: str = ""
    min_ram_gb: int = 8
    tags: list[str] | None = None


def get_hardware() -> HardwareInfo:
    """Get cached hardware info."""
    global _cached_hw
    if _cached_hw is None:
        _cached_hw = detect_hardware()
    return _cached_hw


def get_presets() -> list[dict]:
    """Return top hardware-ranked recommendations as presets."""
    hw = get_hardware()
    recs = get_recommendations(hw)
    return [
        {
            "id": f"rec-{i}",
            "label": r["label"],
            "description": r["description"],
            "repo_id": r["repo_id"],
            "include": r["include"],
            "ctx_size": 4096,
            "threads": 0,
            "gpu_layers": r.get("gpu_layers", 0),
            "extra_args": "",
            "min_ram_gb": int(r.get("estimated_memory_gb", 4)) + 2,
            "tags": r["tags"],
            "builtin": True,
            "score": r["score"],
            "speed": r.get("speed", "unknown"),
            "download_size_gb": r.get("download_size_gb", 0),
            "estimated_memory_gb": r.get("estimated_memory_gb", 0),
            "notes": r.get("notes", ""),
        }
        for i, r in enumerate(recs[:7])
    ]


def get_preset_by_id(preset_id: str) -> ServePreset | None:
    """Look up a preset by ID from recommendations."""
    hw = get_hardware()
    recs = get_recommendations(hw)
    for i, r in enumerate(recs):
        if f"rec-{i}" == preset_id:
            return ServePreset(
                id=preset_id,
                label=r["label"],
                description=r["description"],
                repo_id=r["repo_id"],
                include=r["include"],
                ctx_size=4096,
                gpu_layers=r.get("gpu_layers", 0),
                tags=r["tags"],
            )
    return None


def get_ranked_recommendations(ctx_size: int = 4096) -> list[dict]:
    """Get all models ranked by hardware compatibility."""
    hw = get_hardware()
    return get_recommendations(hw, ctx_size=ctx_size)


def get_hardware_summary() -> dict:
    """Get hardware info for display."""
    hw = get_hardware()
    return hw.to_dict()
