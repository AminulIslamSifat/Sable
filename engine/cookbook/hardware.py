
"""Hardware detection and model compatibility scoring."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path

logger = logging.getLogger("sable.cookbook")


@dataclass
class HardwareInfo:
    """Detected system hardware relevant to LLM serving."""
    total_ram_gb: float
    available_ram_gb: float
    cpu_cores: int
    cpu_threads: int
    gpu_name: str | None
    gpu_vram_gb: float
    gpu_backend: str  # "cuda", "vulkan", "none"
    disk_free_gb: float
    swap_total_gb: float

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def usable_memory_gb(self) -> float:
        """Effective memory for model loading.
        Uses total RAM minus 4GB system reserve (not current available),
        since user can close other apps before running a model.
        """
        return max(self.total_ram_gb - 4.0, 2.0) + self.gpu_vram_gb

    @property
    def can_offload(self) -> bool:
        """Whether GPU offloading is possible."""
        return self.gpu_vram_gb > 0.5


def detect_hardware() -> HardwareInfo:
    """Detect system hardware specs."""
    # RAM
    total_ram = _get_total_ram()
    avail_ram = _get_available_ram()

    # CPU
    cpu_cores = os.cpu_count() or 4
    cpu_threads = cpu_cores  # On Linux, cpu_count gives threads

    # GPU
    gpu_name, gpu_vram, gpu_backend = _detect_gpu()

    # Disk
    from engine.cookbook.state import get_state
    models_dir = get_state().models_dir
    try:
        disk = shutil.disk_usage(str(models_dir if models_dir.exists() else "/"))
        disk_free = disk.free / (1024 ** 3)
    except OSError:
        disk_free = 0

    # Swap
    swap_total = _get_swap_total()

    info = HardwareInfo(
        total_ram_gb=round(total_ram, 1),
        available_ram_gb=round(avail_ram, 1),
        cpu_cores=cpu_cores,
        cpu_threads=cpu_threads,
        gpu_name=gpu_name,
        gpu_vram_gb=round(gpu_vram, 1),
        gpu_backend=gpu_backend,
        disk_free_gb=round(disk_free, 1),
        swap_total_gb=round(swap_total, 1),
    )
    logger.info("Hardware detected: %s", info)
    return info


def _get_total_ram() -> float:
    """Total RAM in GB."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / (1024 ** 2)
    except (OSError, ValueError, IndexError):
        pass
    return 8.0  # fallback


def _get_available_ram() -> float:
    """Available RAM in GB."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / (1024 ** 2)
    except (OSError, ValueError, IndexError):
        pass
    return 4.0


def _get_swap_total() -> float:
    """Total swap in GB."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("SwapTotal:"):
                    return int(line.split()[1]) / (1024 ** 2)
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def _detect_gpu() -> tuple[str | None, float, str]:
    """Detect GPU name, VRAM in GB, and backend."""
    # Try NVIDIA
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            line = result.stdout.strip().split("\n")[0]
            parts = line.split(", ")
            name = parts[0].strip()
            vram_mb = float(parts[1].strip())
            return name, vram_mb / 1024, "cuda"
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, IndexError):
        pass

    # Try Vulkan (for AMD/Intel)
    try:
        result = subprocess.run(
            ["vulkaninfo", "--summary"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            # Parse device name from vulkaninfo
            for line in result.stdout.split("\n"):
                if "deviceName" in line:
                    name = line.split("=")[-1].strip()
                    return name, 0, "vulkan"  # Can't easily get VRAM from vulkaninfo
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return None, 0.0, "none"


# ─── Model Compatibility Scoring ──────────────────────────────────────────────

# Approximate memory usage per billion parameters by quantization
# (in GB, including KV cache overhead) — used as fallback for RAM estimation
_QUANT_MEMORY_PER_B = {
    "Q2_K": 0.35,
    "Q3_K_M": 0.45,
    "Q4_0": 0.55,
    "Q4_K_M": 0.60,
    "Q4_K_S": 0.58,
    "Q5_K_M": 0.70,
    "Q5_K_S": 0.68,
    "Q6_K": 0.80,
    "Q8_0": 1.05,
    "F16": 2.0,
}


def estimate_memory_gb(params_b: float, quant: str, ctx_size: int = 4096) -> float:
    """Estimate memory needed for a model at given quant and context."""
    base = params_b * _QUANT_MEMORY_PER_B.get(quant, 0.60)
    # KV cache: ~0.4GB for 8B at 4096 ctx (scales linearly with ctx and params)
    kv_overhead = (ctx_size / 4096) * params_b * 0.05
    return base + kv_overhead


def score_model_for_hardware(
    hw: HardwareInfo,
    params_b: float,
    quant: str,
    ctx_size: int = 4096,
    repo_id: str = "",
) -> dict:
    """Score a model configuration for the detected hardware.

    Returns {score: 0-100, fits: bool, gpu_layers: int, notes: str, download_size_gb}
    """
    mem_needed = estimate_memory_gb(params_b, quant, ctx_size)
    available = hw.usable_memory_gb

    # Estimate download size from formula (no network calls)
    download_size_gb = params_b * _QUANT_MEMORY_PER_B.get(quant, 0.60)

    # Check disk space
    if download_size_gb > hw.disk_free_gb:
        return {
            "score": 0, "fits": False, "gpu_layers": 0,
            "estimated_memory_gb": round(mem_needed, 1),
            "download_size_gb": round(download_size_gb, 2),
            "notes": f"Not enough disk space (need ~{download_size_gb:.1f} GB)",
        }

    # Check memory
    if mem_needed > available * 1.2:  # Allow 20% overcommit with swap
        return {
            "score": 0, "fits": False, "gpu_layers": 0,
            "estimated_memory_gb": round(mem_needed, 1),
            "download_size_gb": round(download_size_gb, 2),
            "notes": f"Needs ~{mem_needed:.1f} GB, only {available:.1f} GB available",
        }

    # Calculate GPU offload layers
    gpu_layers = 0
    if hw.gpu_vram_gb > 0.5:
        # Estimate how many layers fit in VRAM
        # Rough: each layer uses ~params_b * 0.02 GB at Q4
        layer_mem = params_b * 0.025
        if layer_mem > 0:
            gpu_layers = min(int(hw.gpu_vram_gb / layer_mem), 99)

    # Score based on how comfortably it fits
    ratio = available / mem_needed
    if ratio >= 2.0:
        score = 100
        notes = "Runs comfortably with plenty of headroom"
    elif ratio >= 1.5:
        score = 85
        notes = "Good fit with moderate headroom"
    elif ratio >= 1.2:
        score = 70
        notes = "Fits but may be tight under load"
    elif ratio >= 1.0:
        score = 50
        notes = "Barely fits — expect slowdowns"
    else:
        score = 30
        notes = "Will use swap — very slow"

    # Bonus for GPU offload
    if gpu_layers > 0:
        score = min(100, score + 10)
        notes += f" · GPU offload: ~{gpu_layers} layers"

    # Speed estimate
    if hw.gpu_backend == "cuda" and gpu_layers > 20:
        speed = "fast"
    elif hw.cpu_cores >= 8:
        speed = "moderate"
    else:
        speed = "slow"

    return {
        "score": score,
        "fits": True,
        "gpu_layers": gpu_layers,
        "estimated_memory_gb": round(mem_needed, 1),
        "download_size_gb": round(download_size_gb, 2),
        "speed": speed,
        "notes": notes,
    }


# ─── Model Catalog ────────────────────────────────────────────────────────────
# Dynamic catalog — can be extended or fetched from HF

MODEL_CATALOG = [
    # (repo_id, label, params_b, quants available, tags, description)
    {
        "repo_id": "Qwen/Qwen2.5-0.5B-Instruct-GGUF",
        "label": "Qwen 2.5 0.5B",
        "params_b": 0.5,
        "quants": ["q4_k_m", "q8_0"],
        "tags": ["tiny", "fast", "testing"],
        "description": "Ultra-light. Good for testing the pipeline.",
    },
    {
        "repo_id": "Qwen/Qwen2.5-1.5B-Instruct-GGUF",
        "label": "Qwen 2.5 1.5B",
        "params_b": 1.5,
        "quants": ["q4_k_m", "q5_k_m", "q8_0"],
        "tags": ["small", "fast"],
        "description": "Light and quick. Decent for simple tasks.",
    },
    {
        "repo_id": "Qwen/Qwen2.5-3B-Instruct-GGUF",
        "label": "Qwen 2.5 3B",
        "params_b": 3,
        "quants": ["q4_k_m", "q5_k_m", "q6_k", "q8_0"],
        "tags": ["small", "balanced"],
        "description": "Good balance of speed and quality for coding.",
    },
    {
        "repo_id": "Qwen/Qwen2.5-7B-Instruct-GGUF",
        "label": "Qwen 2.5 7B",
        "params_b": 7,
        "quants": ["q4_k_m", "q5_k_m", "q6_k"],
        "tags": ["medium", "coding"],
        "description": "Strong coding and reasoning. Best 7B for general use.",
    },
    {
        "repo_id": "Qwen/Qwen2.5-14B-Instruct-GGUF",
        "label": "Qwen 2.5 14B",
        "params_b": 14,
        "quants": ["q4_k_m", "q5_k_m"],
        "tags": ["large", "coding", "reasoning"],
        "description": "Excellent quality. Needs 16GB+ RAM.",
    },
    {
        "repo_id": "Qwen/Qwen2.5-32B-Instruct-GGUF",
        "label": "Qwen 2.5 32B",
        "params_b": 32,
        "quants": ["q4_k_m", "q3_k_m"],
        "tags": ["xl", "reasoning"],
        "description": "Near-flagship quality. Needs 32GB+ RAM.",
    },
    {
        "repo_id": "huggingface/llama-3.2-3b-instruct.Q4_K_M-GGUF",
        "label": "Llama 3.2 3B",
        "params_b": 3,
        "quants": ["q4_k_m"],
        "tags": ["small", "balanced"],
        "description": "Meta's efficient 3B. Good instruction following.",
    },
    {
        "repo_id": "bartowski/Llama-3.1-8B-Instruct-GGUF",
        "label": "Llama 3.1 8B",
        "params_b": 8,
        "quants": ["q4_k_m", "q5_k_m", "q6_k"],
        "tags": ["medium", "general"],
        "description": "Strong general-purpose 8B model.",
    },
    {
        "repo_id": "TheBloke/Mistral-7B-Instruct-v0.3-GGUF",
        "label": "Mistral 7B v0.3",
        "params_b": 7,
        "quants": ["q4_k_m", "q5_k_m", "q6_k"],
        "tags": ["medium", "fast"],
        "description": "Fast inference, good at following instructions.",
    },
    {
        "repo_id": "microsoft/Phi-3-mini-4k-instruct-gguf",
        "label": "Phi-3 Mini 3.8B",
        "params_b": 3.8,
        "quants": ["q4_k_m"],
        "tags": ["small", "reasoning"],
        "description": "Microsoft's reasoning-focused small model.",
    },
    {
        "repo_id": "bartowski/DeepSeek-R1-Distill-Qwen-7B-GGUF",
        "label": "DeepSeek R1 7B",
        "params_b": 7,
        "quants": ["q4_k_m", "q5_k_m"],
        "tags": ["medium", "reasoning", "thinking"],
        "description": "Chain-of-thought reasoning model. Great for math/logic.",
    },
    {
        "repo_id": "bartowski/DeepSeek-R1-Distill-Qwen-14B-GGUF",
        "label": "DeepSeek R1 14B",
        "params_b": 14,
        "quants": ["q4_k_m", "q5_k_m"],
        "tags": ["large", "reasoning", "thinking"],
        "description": "Larger R1 distill. Better reasoning, needs more RAM.",
    },
    {
        "repo_id": "bartowski/gemma-2-2b-it-GGUF",
        "label": "Gemma 2 2B",
        "params_b": 2,
        "quants": ["q4_k_m", "q5_k_m"],
        "tags": ["tiny", "fast"],
        "description": "Google's compact model. Surprisingly capable for size.",
    },
    {
        "repo_id": "bartowski/gemma-2-9b-it-GGUF",
        "label": "Gemma 2 9B",
        "params_b": 9,
        "quants": ["q4_k_m", "q5_k_m"],
        "tags": ["medium", "general"],
        "description": "Google's 9B. Strong at creative and analytical tasks.",
    },
    {
        "repo_id": "QuantFactory/Codestral-22B-v0.1-GGUF",
        "label": "Codestral 22B",
        "params_b": 22,
        "quants": ["q4_k_m", "q3_k_m"],
        "tags": ["xl", "coding"],
        "description": "Mistral's code specialist. Excellent for programming.",
    },
    # ─── Gemma 3 / 3n / 4 ───────────────────────────────────────────────────
    {
        "repo_id": "MaziyarPanahi/gemma-3-4b-it-GGUF",
        "label": "Gemma 3 4B IT",
        "params_b": 4,
        "quants": ["q4_k_m", "q5_k_m", "q6_k", "q8_0"],
        "tags": ["small", "balanced", "multimodal"],
        "description": "Google's Gemma 3 4B. Strong multimodal + reasoning for its size.",
    },
    {
        "repo_id": "bartowski/google_gemma-3n-E4B-it-GGUF",
        "label": "Gemma 3n E4B IT",
        "params_b": 4,
        "quants": ["q4_k_m", "q5_k_m", "q8_0"],
        "tags": ["small", "efficient", "on-device"],
        "description": "Google's efficient E4B variant. Designed for on-device with 4B-level quality.",
    },
    {
        "repo_id": "daniloreddy/gemma-4-E4B-it_GGUF",
        "label": "Gemma 4 E4B IT",
        "params_b": 4,
        "quants": ["q4_k_m", "q5_k_m", "q8_0"],
        "tags": ["small", "efficient", "latest"],
        "description": "Google's latest Gemma 4 E4B. Next-gen efficiency and instruction following.",
    },
]

# Keep backward compat alias
STATIC_CATALOG = MODEL_CATALOG

# ─── Dynamic HF Model Fetching ────────────────────────────────────────────────

_dynamic_cache: list[dict] | None = None
_dynamic_cache_time: float = 0

_QUANT_PATTERNS = [
    "q2_k", "q3_k_m", "q3_k_s", "q4_0", "q4_k_m", "q4_k_s",
    "q5_0", "q5_k_m", "q5_k_s", "q6_k", "q8_0", "f16",
]


def _fetch_hf_models(limit: int = 40) -> list[dict]:
    """Fetch popular GGUF models from HuggingFace API."""
    import json
    import re
    import urllib.request

    url = (
        "https://huggingface.co/api/models"
        "?search=gguf&sort=downloads&direction=-1"
        f"&limit={limit}&filter=text-generation"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Sable-Cookbook/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:
        logger.warning("Failed to fetch HF models: %s", exc)
        return []

    results = []
    seen_ids = {m["repo_id"] for m in MODEL_CATALOG}

    for item in data:
        repo_id = item.get("modelId", "")
        if not repo_id or repo_id in seen_ids:
            continue
        # Must look like a GGUF repo
        if "gguf" not in repo_id.lower():
            continue

        # Parse model size from name
        name = repo_id.split("/")[-1]
        params_b = None
        m = re.search(r"(\d+(?:\.\d+)?)\s*[Bb]", name)
        if m:
            params_b = float(m.group(1))
        if params_b is None or params_b < 0.1 or params_b > 200:
            continue

        # Detect quants from file list
        quants = set()
        for sib in item.get("siblings", []):
            fname = sib.get("rfilename", "").lower()
            if fname.endswith(".gguf"):
                for q in _QUANT_PATTERNS:
                    if q in fname:
                        quants.add(q)
        if not quants:
            quants = {"q4_k_m"}

        # Clean label
        label = name
        for suffix in ["-GGUF", "-gguf", "_GGUF", ".GGUF"]:
            label = label.replace(suffix, "")
        label = label.replace("-", " ").replace("_", " ").strip()
        if len(label) > 40:
            label = label[:37] + "..."

        results.append({
            "repo_id": repo_id,
            "label": label,
            "params_b": params_b,
            "quants": sorted(quants),
            "tags": ["community"],
            "description": f"{item.get('downloads', 0):,} downloads",
        })
        seen_ids.add(repo_id)

    logger.info("Fetched %d dynamic models from HF", len(results))
    return results


def get_model_catalog() -> list[dict]:
    """Get full catalog: static + dynamic from HF (cached 1 hour)."""
    global _dynamic_cache, _dynamic_cache_time
    import time

    now = time.time()
    if _dynamic_cache is None or (now - _dynamic_cache_time) > 3600:
        _dynamic_cache = _fetch_hf_models(limit=40)
        _dynamic_cache_time = now

    seen = {m["repo_id"] for m in MODEL_CATALOG}
    merged = list(MODEL_CATALOG)
    for m in (_dynamic_cache or []):
        if m["repo_id"] not in seen:
            merged.append(m)
            seen.add(m["repo_id"])
    return merged


def get_recommendations(hw: HardwareInfo, ctx_size: int = 4096) -> list[dict]:
    """Rank all catalog models by hardware compatibility.

    Returns ALL models (static + dynamic from HF) sorted by score.
    Incompatible models are included with fits=False for red display.
    """
    results = []
    catalog = get_model_catalog()
    for model in catalog:
        # Try all quants, pick the best one (even if it doesn't fit)
        best = None
        for quant in model["quants"]:
            quant_upper = quant.upper()
            scoring = score_model_for_hardware(
                hw, model["params_b"], quant_upper, ctx_size,
                repo_id=model["repo_id"],
            )
            if best is None or scoring["score"] > best["score"]:
                best = {**scoring, "quant": quant}

        if best:
            include = f"*{best['quant']}*"
            results.append({
                "repo_id": model["repo_id"],
                "label": model["label"],
                "params_b": model["params_b"],
                "quant": best["quant"],
                "include": include,
                "tags": model["tags"],
                "description": model["description"],
                "score": best["score"],
                "fits": best["fits"],
                "estimated_memory_gb": best.get("estimated_memory_gb", 0),
                "download_size_gb": best.get("download_size_gb", 0),
                "gpu_layers": best.get("gpu_layers", 0),
                "speed": best.get("speed", "unknown"),
                "notes": best["notes"],
            })

    # Sort: compatible first (by score desc), then incompatible (by params asc)
    results.sort(key=lambda x: (x["fits"], x["score"], -x["params_b"]), reverse=True)
    return results
