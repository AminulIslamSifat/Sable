#!/usr/bin/env python3
"""Advanced SDXL — AI Image Generator via Perchance backend.

Perchance "Advanced SDXL Generator" with model types, quality→steps+cfg mapping,
composition control, style presets, lighting modes, and aspect ratios.

Usage (CLI):
    python3 advanced_sdxl.py generate --prompt "..." [options]
    python3 advanced_sdxl.py options  # print all available options as JSON

Usage (import):
    from tools.image_generator.scripts.advanced_sdxl import generate, AdvancedSdxlOptions
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

try:
    import httpx
except ImportError:
    print("Error: httpx not installed. Run: pip install httpx", file=sys.stderr)
    sys.exit(1)

# ─── Constants ───────────────────────────────────────────────────────────────

BASE_URL = "https://image-generation.perchance.org/api"
CHANNEL = "advanced-sdxl-generator"
AD_ACCESS_CODE = "09c1642ec4202067172b320731ede6afa83b3d77917498fd8b92add6a9915180"
KEY_CACHE_FILE = Path(__file__).resolve().parent.parent.parent.parent / "system" / ".advanced_sdxl_key"
OUTPUT_DIR = Path.home() / "sable_output" / "assets"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "Referer": "https://image-generation.perchance.org/embed",
    "Content-Type": "text/plain;charset=UTF-8",
}

DEFAULT_NEGATIVE_PROMPT = (
    "blurry, pixelated, low quality, bad anatomy, distorted face, "
    "asymmetrical features, unnatural skin tones, incorrect proportions, "
    "extra limbs, deformed hands, missing fingers, poorly rendered eyes, "
    "unnatural shadows, overexposed, underexposed, dull colors, low contrast, "
    "grainy, watermark, text, logo"
)

# ─── Option Catalogs ─────────────────────────────────────────────────────────

MODEL_TYPES: dict[str, str] = {
    "sdxl-base": "SDXL Standard (Fast)",
    "sdxl-refiner": "SDXL + Refiner (Detailed)",
    "sdxl-lightning": "SDXL Lightning (Hyper-Fast)",
    "pony-diffusion": "Pony Diffusion V6 (Artistic/Char)",
    "juggernaut-xl": "Juggernaut XL (Realistic/Photo)",
}

# Quality maps to (steps_note, cfg_scale). Steps are server-side only.
QUALITY_LEVELS: dict[str, dict[str, Any]] = {
    "fast": {"label": "Fast (Draft)", "steps": 20, "cfg": 7.0},
    "balanced": {"label": "Balanced (Standard)", "steps": 30, "cfg": 7.5},
    "high": {"label": "High Quality (Fine Details)", "steps": 50, "cfg": 8.0},
    "ultra": {"label": "Ultra HD (Complex Scenes)", "steps": 70, "cfg": 9.0},
    "artistic": {"label": "Artistic (More Freedom)", "steps": 30, "cfg": 4.0},
}

STYLE_PRESETS: dict[str, str] = {
    "none": "",
    "photorealistic": "photorealistic, 8k, hyperdetailed, masterpiece, fujifilm, raw photo",
    "cinematic": "cinematic movie still, anamorphic lens, dramatic atmosphere, professional color grading",
    "analog_film": "analog film style, 35mm, vintage grain, kodak portra 400",
    "anime": "anime style, vibrant colors, cel shading, studio ghibli aesthetic",
    "digital_art": "digital art, concept art, trending on artstation, sharp focus",
    "oil_painting": "oil painting, heavy brush strokes, canvas texture, masterpiece",
    "watercolor": "watercolor, soft edges, dreamy, ink wash, artistic bleed",
    "fantasy": "fantasy art, magical atmosphere, intricate details, ethereal",
    "comic_book": "comic book style, bold lines, halftone dots, high contrast",
    "cyberpunk": "cyberpunk, neon lights, futuristic city, rainy street, high-tech",
    "3d_disney": "3d render, octane render, unreal engine 5, pixar style, cute character",
    "pixel_art": "pixel art, 16-bit, retro gaming aesthetic, high quality pixel",
    "origami": "origami style, folded paper, paper texture, handcrafted",
    "low_poly": "low poly, geometric shapes, minimalist art, game design",
    "architecture": "architectural visualization, modern interior, minimalist design, luxury, photoreal",
    "pencil_sketch": "pencil sketch, graphite, hand-drawn, paper texture, charcoal",
}

LIGHTING_MODES: dict[str, str] = {
    "none": "",
    "golden_hour": "golden hour, warm sunset lighting, soft shadows",
    "studio_soft": "soft diffused lighting, professional studio setup, high-key",
    "dramatic": "dramatic lighting, chiaroscuro, heavy contrast, mystery",
    "neon": "neon lighting, cyberpunk glow, blue and pink hues",
    "cinematic": "cinematic lighting, volumetric fog, rim lighting, moody",
    "sunlight": "natural sunlight, outdoor shadows, clear sky",
    "moonlight": "moonlight, cold blue tones, night atmosphere, ethereal glow",
    "bioluminescence": "bioluminescent glow, mystical light, internal illumination",
    "god_rays": "god rays, sunbeams through trees, ethereal lighting, holy",
}

COMPOSITION_MODES: dict[str, str] = {
    "none": "",
    "close_up": "close up portrait, head and shoulders, detailed face",
    "full_body": "full body shot, standing, from head to toe",
    "wide_angle": "wide angle shot, landscape, panoramic view, vast environment",
    "low_angle": "low angle shot, looking up, heroic perspective, imposing",
    "high_angle": "high angle shot, looking down, bird's eye view",
    "eye_level": "eye level shot, straight on, natural perspective",
    "dutch_angle": "dutch angle, tilted frame, uneasy, cinematic tension",
    "macro": "macro shot, extreme close-up, tiny details, bokeh background",
    "shallow_dof": "depth of field, blurred background, sharp foreground, bokeh",
    "rule_of_thirds": "rule of thirds, balanced composition, aesthetic placement",
    "symmetrical": "symmetrical composition, centered, balanced, harmonic",
    "dynamic_action": "dynamic action shot, mid-motion, energy, speed lines",
}

ASPECT_RATIOS: dict[str, tuple[int, int]] = {
    "square": (1024, 1024),
    "portrait": (832, 1216),
    "landscape": (1216, 832),
    "widescreen": (1344, 768),
    "story": (768, 1344),
    "ultrawide": (1536, 640),
}

BATCH_SIZES = [1, 2, 4]


# ─── Dataclass ────────────────────────────────────────────────────────────────

@dataclass
class AdvancedSdxlOptions:
    prompt: str = ""
    negative_prompt: str = ""
    model_type: str = "sdxl-base"
    quality: str = "balanced"
    style: str = "none"
    lighting: str = "none"
    composition: str = "none"
    aspect_ratio: str = "square"
    seed: int = -1
    batch_size: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AdvancedSdxlOptions:
        valid = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in valid}
        return cls(**filtered)


# ─── Key Management (delegated to perchance_key module) ─────────────────────

import sys as _sys
if str(Path(__file__).resolve().parent) not in _sys.path:
    _sys.path.insert(0, str(Path(__file__).resolve().parent))

from perchance_key import (
    get_valid_key as _shared_get_valid_key,
    save_key as _shared_save_key,
    load_cached_key as _load_cached_key,
    verify_key as _verify_key,
)


def _save_key(key: str) -> None:
    _shared_save_key(key, KEY_CACHE_FILE)


def get_valid_key() -> str | None:
    """Get a valid userKey via shared module (browser-based refresh)."""
    try:
        return _shared_get_valid_key(
            KEY_CACHE_FILE,
            env_var="PERCHANCE_KEY",
            tag="advanced-sdxl",
        )
    except RuntimeError as e:
        print(f"[advanced-sdxl][key] Failed: {e}", file=sys.stderr)
        return None


# ─── Prompt Building ─────────────────────────────────────────────────────────

def build_prompt(opts: AdvancedSdxlOptions) -> str:
    parts = [opts.prompt.strip()]

    style_kw = STYLE_PRESETS.get(opts.style, "")
    if style_kw:
        parts.append(style_kw)

    lighting_kw = LIGHTING_MODES.get(opts.lighting, "")
    if lighting_kw:
        parts.append(lighting_kw)

    comp_kw = COMPOSITION_MODES.get(opts.composition, "")
    if comp_kw:
        parts.append(comp_kw)

    parts.append("best quality, masterpiece")
    return ", ".join(p for p in parts if p)


def build_negative_prompt(opts: AdvancedSdxlOptions) -> str:
    user_neg = opts.negative_prompt.strip()
    if user_neg:
        return f"{DEFAULT_NEGATIVE_PROMPT}, {user_neg}"
    return DEFAULT_NEGATIVE_PROMPT


# ─── API Calls ────────────────────────────────────────────────────────────────

def _call_generate(
    user_key: str,
    prompt: str,
    negative_prompt: str,
    resolution: str,
    seed: int,
    guidance_scale: float,
) -> dict[str, Any]:
    body = {
        "prompt": prompt,
        "negativePrompt": negative_prompt,
        "seed": seed,
        "resolution": resolution,
        "guidanceScale": guidance_scale,
        "channel": CHANNEL,
        "subChannel": "public",
        "userKey": user_key,
        "adAccessCode": AD_ACCESS_CODE,
        "requestId": str(random.random()),
    }

    url = (
        f"{BASE_URL}/generate"
        f"?userKey={user_key}"
        f"&requestId={str(random.random())}"
        f"&adAccessCode={AD_ACCESS_CODE}"
        f"&v=asdxl1"
    )

    resp = httpx.post(url, content=json.dumps(body), headers=HEADERS, timeout=120)
    return resp.json()


def _download_image(download_url: str, seed: int, w: int, h: int) -> dict[str, Any]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    filename = f"advanced_sdxl_{ts}_{seed}_{w}x{h}.jpeg"
    filepath = OUTPUT_DIR / filename

    resp = httpx.get(download_url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    filepath.write_bytes(resp.content)

    return {
        "filename": filename,
        "path": str(filepath),
        "size_bytes": len(resp.content),
        "seed": seed,
        "dimensions": f"{w}x{h}",
    }


# ─── Main Generate Function ──────────────────────────────────────────────────

def generate(opts: AdvancedSdxlOptions) -> dict[str, Any]:
    """Generate image(s) with Advanced SDXL options.

    Returns dict with keys: ok, images, count, error, prompt_used, options.
    On key failure, auto-refreshes and retries once.
    """
    if not opts.prompt.strip():
        return {"ok": False, "error": "prompt is required", "images": [], "count": 0}

    final_prompt = build_prompt(opts)
    final_negative = build_negative_prompt(opts)

    w, h = ASPECT_RATIOS.get(opts.aspect_ratio, (1024, 1024))
    resolution = f"{w}x{h}"

    quality_cfg = QUALITY_LEVELS.get(opts.quality, QUALITY_LEVELS["balanced"])["cfg"]

    user_key = get_valid_key()
    if not user_key:
        return {"ok": False, "error": "Failed to obtain API key", "images": [], "count": 0}

    images: list[dict[str, Any]] = []
    errors: list[str] = []
    batch = max(1, min(opts.batch_size, 4))

    for i in range(batch):
        seed = opts.seed if opts.seed != -1 else random.randint(1, 2**31)

        try:
            result = _call_generate(
                user_key=user_key,
                prompt=final_prompt,
                negative_prompt=final_negative,
                resolution=resolution,
                seed=seed,
                guidance_scale=quality_cfg,
            )
        except Exception as e:
            errors.append(f"Image {i+1}: network error — {e}")
            continue

        status = result.get("status", "")

        # Key failure → refresh and retry once
        if status in ("invalid_key", "failed_verification"):
            print(f"[advanced-sdxl] Key invalid on attempt {i+1}, refreshing via browser...", file=sys.stderr)
            from perchance_key import refresh_key_via_browser
            new_key = refresh_key_via_browser(tag="advanced-sdxl")
            if new_key:
                _save_key(new_key)
            if new_key:
                user_key = new_key
                try:
                    result = _call_generate(
                        user_key=user_key,
                        prompt=final_prompt,
                        negative_prompt=final_negative,
                        resolution=resolution,
                        seed=seed,
                        guidance_scale=quality_cfg,
                    )
                    status = result.get("status", "")
                except Exception as e:
                    errors.append(f"Image {i+1}: retry failed — {e}")
                    continue
            else:
                errors.append(f"Image {i+1}: key refresh failed")
                continue

        # Rate limiting
        msg_lower = str(result.get("message", "")).lower()
        if status == "rate_limited" or "limit reached" in msg_lower or "rate limit" in msg_lower:
            err_msg = result.get("message", result.get("error", "Rate limited"))
            errors.append(f"Image {i+1}: RATE LIMITED — {err_msg}")
            break

        # Other errors
        if status == "error" or "error" in result:
            err_msg = result.get("error", result.get("message", str(result)))
            errors.append(f"Image {i+1}: {err_msg}")
            continue

        # Extract download URL (may be relative path)
        download_url = result.get("imageDownloadUrl", "")
        if not download_url:
            download_url = result.get("url", result.get("image", ""))
        if not download_url:
            errors.append(f"Image {i+1}: no download URL in response")
            continue
        # Prepend base URL if relative path
        if download_url.startswith("/"):
            download_url = "https://image-generation.perchance.org" + download_url

        # Download
        try:
            img_info = _download_image(download_url, seed, w, h)
            images.append(img_info)
        except Exception as e:
            errors.append(f"Image {i+1}: download failed — {e}")

    return {
        "ok": len(images) > 0,
        "images": images,
        "count": len(images),
        "errors": errors if errors else None,
        "prompt_used": final_prompt,
        "negative_prompt_used": final_negative,
        "options": opts.to_dict(),
        "provider": "advanced-sdxl",
    }


# ─── Options Export ───────────────────────────────────────────────────────────

def get_all_options() -> dict[str, Any]:
    """Return all available options as a structured dict for the frontend."""
    return {
        "model_types": {k: v for k, v in MODEL_TYPES.items()},
        "quality_levels": {
            k: {"label": v["label"], "steps": v["steps"], "cfg": v["cfg"]}
            for k, v in QUALITY_LEVELS.items()
        },
        "style_presets": {k: v for k, v in STYLE_PRESETS.items()},
        "lighting_modes": {k: v for k, v in LIGHTING_MODES.items()},
        "composition_modes": {k: v for k, v in COMPOSITION_MODES.items()},
        "aspect_ratios": {
            k: {"width": w, "height": h, "label": _ratio_label(k)}
            for k, (w, h) in ASPECT_RATIOS.items()
        },
        "batch_sizes": BATCH_SIZES,
        "default_negative_prompt": DEFAULT_NEGATIVE_PROMPT,
    }


def _ratio_label(key: str) -> str:
    labels = {
        "square": "1:1 Square",
        "portrait": "2:3 Portrait",
        "landscape": "3:2 Landscape",
        "widescreen": "16:9 Widescreen",
        "story": "9:16 Story/TikTok",
        "ultrawide": "21:9 Ultrawide",
    }
    return labels.get(key, key)


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Advanced SDXL AI Image Generator")
    sub = parser.add_subparsers(dest="command")

    gen = sub.add_parser("generate", help="Generate image(s)")
    gen.add_argument("--prompt", required=True, help="Image description")
    gen.add_argument("--negative-prompt", default="", help="What to avoid")
    gen.add_argument("--model-type", default="sdxl-base", choices=list(MODEL_TYPES.keys()))
    gen.add_argument("--quality", default="balanced", choices=list(QUALITY_LEVELS.keys()))
    gen.add_argument("--style", default="none", choices=list(STYLE_PRESETS.keys()))
    gen.add_argument("--lighting", default="none", choices=list(LIGHTING_MODES.keys()))
    gen.add_argument("--composition", default="none", choices=list(COMPOSITION_MODES.keys()))
    gen.add_argument("--aspect-ratio", default="square", choices=list(ASPECT_RATIOS.keys()))
    gen.add_argument("--seed", type=int, default=-1, help="Seed (-1 for random)")
    gen.add_argument("--batch-size", type=int, default=1, choices=BATCH_SIZES)

    sub.add_parser("options", help="Print all options as JSON")

    args = parser.parse_args()

    if args.command == "options":
        print(json.dumps(get_all_options(), indent=2))
        return

    if args.command == "generate":
        opts = AdvancedSdxlOptions(
            prompt=args.prompt,
            negative_prompt=args.negative_prompt,
            model_type=args.model_type,
            quality=args.quality,
            style=args.style,
            lighting=args.lighting,
            composition=args.composition,
            aspect_ratio=args.aspect_ratio,
            seed=args.seed,
            batch_size=args.batch_size,
        )
        result = generate(opts)
        print(json.dumps(result, indent=2))
        sys.exit(0 if result["ok"] else 1)

    parser.print_help()


if __name__ == "__main__":
    main()
