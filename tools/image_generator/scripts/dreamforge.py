#!/usr/bin/env python3
"""DreamForge — AI Image Generator via Perchance backend.

Full-featured image generation with art styles, quality levels, lighting,
atmosphere, enhancements, dimensions, sampling methods, and more.

Usage (CLI):
    python3 dreamforge.py generate --prompt "..." [options]
    python3 dreamforge.py options  # print all available options as JSON

Usage (import):
    from tools.image_generator.scripts.dreamforge import generate, DreamForgeOptions
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
CHANNEL = "5yf90s8rdo"
AD_ACCESS_CODE = "3edfca68e17690650f599c6ba1a71476b2bab44e27f4fc3907c76dbad206413f"
KEY_CACHE_FILE = Path(__file__).resolve().parent.parent.parent.parent / "system" / ".dreamforge_key"
OUTPUT_DIR = Path.home() / "sable_output" / "assets"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "Referer": "https://image-generation.perchance.org/embed",
    "Content-Type": "text/plain;charset=UTF-8",
}

# ─── Option Catalogs ─────────────────────────────────────────────────────────

ART_STYLES: dict[str, str] = {
    "photorealistic": "photorealistic, hyperrealistic, 8k uhd, professional photography, detailed",
    "cinematic": "cinematic, movie still, dramatic composition, film grain",
    "digital_art": "digital art, digital painting, concept art",
    "oil_painting": "oil painting, classical painting, brush strokes, canvas texture",
    "anime": "anime style, manga, cel shading, anime art",
    "fantasy": "fantasy art, magical, ethereal, mystical",
    "sci_fi": "sci-fi, futuristic, science fiction, high tech",
    "3d_render": "3d render, octane render, unreal engine, 3d art",
    "watercolor": "watercolor painting, watercolor art, soft washes",
    "pencil_sketch": "pencil sketch, pencil drawing, graphite, hand drawn",
    "pop_art": "pop art, bold colors, comic style, andy warhol style",
    "vintage": "vintage, retro, aged, nostalgic, old photograph",
    "cyberpunk": "cyberpunk, neon lights, dystopian, high tech low life",
    "steampunk": "steampunk, victorian era, brass, gears, steam powered",
    "surrealism": "surreal, dreamlike, salvador dali style, bizarre",
    "minimalist": "minimalist, simple, clean lines, minimal detail",
    "abstract": "abstract art, non-representational, abstract expressionism",
    "portrait": "portrait photography, studio portrait, headshot",
    "landscape": "landscape photography, wide angle, scenic view",
    "casual_photo": "casual photo, candid, snapshot, natural",
    "no_style": "",
}

QUALITY_LEVELS: dict[str, str] = {
    "masterpiece": "masterpiece, best quality, mastercraft quality, perfect detail, professional grade, 8k uhd",
    "professional": "professional quality, high detail, industry standard, excellent quality",
    "premium": "premium quality, enhanced detail, high quality",
    "standard": "standard quality, good detail",
    "artistic": "artistic quality, stylized detail",
}

LIGHTING: dict[str, str] = {
    "none": "",
    "natural": "natural lighting, perfect exposure, soft shadows",
    "studio": "professional studio lighting, controlled environment, perfect exposure",
    "dramatic": "dramatic lighting, high contrast, mood lighting, shadows",
    "cinematic": "cinematic lighting, film quality, perfect balance, atmospheric",
    "sunset": "golden hour lighting, warm tones, natural glow, orange hues",
    "night": "night lighting, moonlit, atmospheric, dark with highlights",
    "neon": "neon lighting, colorful glows, cyberpunk aesthetic",
    "backlit": "backlit subject, rim lighting, silhouette effect",
}

ATMOSPHERE: dict[str, str] = {
    "none": "",
    "professional": "professional atmosphere, clean, polished",
    "dramatic": "dramatic atmosphere, intense, powerful",
    "peaceful": "peaceful atmosphere, serene, calming",
    "mysterious": "mysterious atmosphere, enigmatic, intriguing",
    "ethereal": "ethereal atmosphere, dreamlike, magical",
    "romantic": "romantic atmosphere, soft, intimate",
    "dynamic": "dynamic atmosphere, energetic, vibrant",
    "nostalgic": "nostalgic atmosphere, vintage feel, timeless",
}

ENHANCEMENT: dict[str, str] = {
    "none": "",
    "hdr": "HDR enhancement, perfect exposure, vivid",
    "soft": "soft focus effect, dreamy quality",
    "sharp": "ultra sharp, crystal clear detail",
    "bokeh": "beautiful bokeh effect, depth of field",
    "glow": "subtle glow effect, ethereal quality",
    "contrast": "enhanced contrast, deep blacks",
    "vibrant": "vibrant colors, rich saturation",
    "film_grain": "subtle film grain, analog feel",
    "detailed": "highly detailed, intricate",
}

DIMENSIONS: dict[str, tuple[int, int]] = {
    "portrait_hd": (512, 768),
    "square_hd": (512, 512),
    "landscape_hd": (768, 512),
    "cinema": (896, 512),
    "vertical": (512, 896),
    "portrait_4k": (1024, 1536),
    "landscape_4k": (1536, 1024),
    "square_4k": (1024, 1024),
}

SAMPLING_METHODS = ["euler_a", "dpm_pp_2m", "dpm_pp_sde", "ddim", "unipc", "dpm3", "plms"]

AI_MODELS = [
    "stable-diffusion-xl", "stable-diffusion-v1-5", "realistic-vision-v4",
    "dreamshaper-8", "deliberate-v2", "anything-v5", "openjourney-v4",
]

# Auto negative prompts per art style
STYLE_NEGATIVES: dict[str, str] = {
    "photorealistic": "cartoon, painting, illustration, drawing, anime, 3d render, low quality",
    "cinematic": "low quality, blurry, amateur, bad composition",
    "digital_art": "photograph, real photo, low quality, blurry",
    "oil_painting": "photograph, digital, low quality, blurry",
    "anime": "photograph, realistic, 3d render, low quality, blurry",
    "fantasy": "low quality, blurry, deformed, ugly",
    "sci_fi": "low quality, blurry, deformed, ugly",
    "3d_render": "photograph, 2d, flat, low quality, blurry",
    "watercolor": "photograph, digital, sharp edges, low quality",
    "pencil_sketch": "photograph, color, digital, low quality",
    "pop_art": "photograph, realistic, low quality, blurry",
    "vintage": "modern, digital, low quality, blurry",
    "cyberpunk": "low quality, blurry, deformed, ugly",
    "steampunk": "low quality, blurry, deformed, ugly",
    "surrealism": "low quality, blurry, deformed, ugly",
    "minimalist": "cluttered, busy, complex, low quality",
    "abstract": "photograph, realistic, figurative, low quality",
    "portrait": "low quality, blurry, deformed, bad anatomy, ugly",
    "landscape": "low quality, blurry, deformed, ugly",
    "casual_photo": "low quality, blurry, deformed, ugly",
    "no_style": "low quality, blurry, deformed, ugly",
}

DEFAULT_NEGATIVE = "low quality, blurry, distorted, deformed, ugly, amateur, bad anatomy"


# ─── Options Dataclass ───────────────────────────────────────────────────────

@dataclass
class DreamForgeOptions:
    prompt: str = ""
    negative_prompt: str = ""
    art_style: str = "photorealistic"
    quality_level: str = "masterpiece"
    lighting: str = "none"
    atmosphere: str = "none"
    enhancement: str = "none"
    dimensions: str = "portrait_hd"
    sampling_method: str = "euler_a"
    ai_model: str = "stable-diffusion-xl"
    seed: int = -1
    guidance_scale: float = 7.0
    batch_size: int = 1
    content_filter: str = "none"  # "pg13" or "none"
    upscale: bool = False
    remove_background: bool = False
    prompt_enhancement: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DreamForgeOptions":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in known}
        return cls(**filtered)


# ─── Prompt Builder ──────────────────────────────────────────────────────────

def build_prompt(opts: DreamForgeOptions) -> str:
    """Construct the final prompt by concatenating user description + option keywords."""
    parts = [opts.prompt.strip()]

    # Art style keywords
    style_kw = ART_STYLES.get(opts.art_style, "")
    if style_kw:
        parts.append(style_kw)

    # Quality keywords
    quality_kw = QUALITY_LEVELS.get(opts.quality_level, "")
    if quality_kw:
        parts.append(quality_kw)

    # Lighting keywords
    light_kw = LIGHTING.get(opts.lighting, "")
    if light_kw:
        parts.append(light_kw)

    # Atmosphere keywords
    atmo_kw = ATMOSPHERE.get(opts.atmosphere, "")
    if atmo_kw:
        parts.append(atmo_kw)

    # Enhancement keywords
    enh_kw = ENHANCEMENT.get(opts.enhancement, "")
    if enh_kw:
        parts.append(enh_kw)

    return ", ".join(p for p in parts if p)


def build_negative_prompt(opts: DreamForgeOptions) -> str:
    """Build negative prompt from style defaults + user input."""
    style_neg = STYLE_NEGATIVES.get(opts.art_style, DEFAULT_NEGATIVE)
    user_neg = opts.negative_prompt.strip()
    if user_neg:
        return f"{style_neg}, {user_neg}"
    return f"{style_neg}, {DEFAULT_NEGATIVE}"


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
            tag="dreamforge",
        )
    except RuntimeError as e:
        print(f"[dreamforge][key] Failed: {e}", file=sys.stderr)
        return None


# ─── Core Generation ─────────────────────────────────────────────────────────

def _call_generate(user_key: str, prompt: str, negative_prompt: str,
                   resolution: str, seed: int, guidance_scale: float) -> dict[str, Any]:
    """Make a single API call to generate an image. Returns raw response dict."""
    request_id = str(random.random())
    cache_bust = str(random.random())

    url = (f"{BASE_URL}/generate"
           f"?userKey={user_key}"
           f"&requestId={request_id}"
           f"&adAccessCode={AD_ACCESS_CODE}"
           f"&__cacheBust={cache_bust}")

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
        "requestId": request_id,
    }

    resp = httpx.post(url, content=json.dumps(body), headers=HEADERS, timeout=90)
    return resp.json()


def _download_image(download_url: str, seed: int, width: int, height: int) -> dict[str, Any]:
    """Download a generated image and save to OUTPUT_DIR."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"df_{int(time.time())}_{seed}.jpg"
    out_path = OUTPUT_DIR / fname

    # The API returns URLs like "/api/downloadTemporaryImageViaProxy?t=..."
    # which need the base domain prepended
    full_url = download_url
    if not full_url.startswith("http"):
        # Ensure no double /api/ prefix
        clean = full_url.lstrip("/")
        full_url = f"https://image-generation.perchance.org/{clean}"

    resp = httpx.get(full_url, headers=HEADERS, timeout=60, follow_redirects=True)
    resp.raise_for_status()
    out_path.write_bytes(resp.content)

    return {
        "ok": True,
        "path": str(out_path),
        "filename": fname,
        "seed": seed,
        "width": width,
        "height": height,
        "size_bytes": len(resp.content),
    }


def generate(opts: DreamForgeOptions) -> dict[str, Any]:
    """Generate image(s) with full DreamForge options.

    Returns dict with keys: ok, images, count, error, prompt_used, options.
    On key failure, auto-refreshes and retries once.
    """
    if not opts.prompt.strip():
        return {"ok": False, "error": "prompt is required", "images": [], "count": 0}

    # Build prompt and negative prompt
    final_prompt = build_prompt(opts)
    final_negative = build_negative_prompt(opts)

    # Resolve dimensions
    w, h = DIMENSIONS.get(opts.dimensions, (512, 768))
    resolution = f"{w}x{h}"

    # Get valid key
    user_key = get_valid_key()
    if not user_key:
        return {"ok": False, "error": "Failed to obtain API key", "images": [], "count": 0}

    # Generate batch
    images: list[dict[str, Any]] = []
    errors: list[str] = []
    batch = max(1, min(opts.batch_size, 15))

    for i in range(batch):
        seed = opts.seed if opts.seed != -1 else random.randint(1, 2**31)

        try:
            result = _call_generate(
                user_key=user_key,
                prompt=final_prompt,
                negative_prompt=final_negative,
                resolution=resolution,
                seed=seed,
                guidance_scale=opts.guidance_scale,
            )
        except Exception as e:
            errors.append(f"Image {i+1}: network error — {e}")
            continue

        # Check for key failure → auto-refresh and retry
        status = result.get("status", "")
        if status in ("invalid_key", "failed_verification"):
            print(f"[dreamforge] Key invalid on attempt {i+1}, refreshing via browser...", file=sys.stderr)
            from perchance_key import refresh_key_via_browser
            new_key = refresh_key_via_browser(tag="dreamforge")
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
                        guidance_scale=opts.guidance_scale,
                    )
                    status = result.get("status", "")
                except Exception as e:
                    errors.append(f"Image {i+1}: retry failed — {e}")
                    continue
            else:
                errors.append(f"Image {i+1}: key refresh failed")
                continue

        # Check for rate limiting
        msg_lower = str(result.get("message", "")).lower()
        if status == "rate_limited" or "limit reached" in msg_lower or "rate limit" in msg_lower:
            err_msg = result.get("message", result.get("error", "Rate limited"))
            errors.append(f"Image {i+1}: RATE LIMITED — {err_msg}")
            # No point continuing the batch if we're rate limited
            break

        # Check for other errors
        if status == "error" or "error" in result:
            err_msg = result.get("error", result.get("message", str(result)))
            errors.append(f"Image {i+1}: {err_msg}")
            continue

        # Extract download URL
        download_url = result.get("imageDownloadUrl", "")
        if not download_url:
            # Try alternative response formats
            download_url = result.get("url", result.get("image", ""))
        if not download_url:
            errors.append(f"Image {i+1}: no download URL in response")
            continue

        # Download the image
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
        "provider": "dreamforge",
    }


# ─── Options Export (for frontend) ──────────────────────────────────────────

def get_all_options() -> dict[str, Any]:
    """Return all available options as a structured dict for the frontend."""
    return {
        "art_styles": {k: v for k, v in ART_STYLES.items()},
        "quality_levels": {k: v for k, v in QUALITY_LEVELS.items()},
        "lighting": {k: v for k, v in LIGHTING.items()},
        "atmosphere": {k: v for k, v in ATMOSPHERE.items()},
        "enhancement": {k: v for k, v in ENHANCEMENT.items()},
        "dimensions": {k: {"width": w, "height": h, "label": _dim_label(k)} for k, (w, h) in DIMENSIONS.items()},
        "sampling_methods": SAMPLING_METHODS,
        "ai_models": AI_MODELS,
        "batch_sizes": [1, 3, 6, 9, 12, 15],
        "content_filters": ["none", "pg13"],
    }


def _dim_label(key: str) -> str:
    labels = {
        "portrait_hd": "Portrait HD", "square_hd": "Square HD",
        "landscape_hd": "Landscape HD", "cinema": "Cinema",
        "vertical": "Vertical", "portrait_4k": "Portrait 4K",
        "landscape_4k": "Landscape 4K", "square_4k": "Square 4K",
    }
    return labels.get(key, key)


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="DreamForge AI Image Generator")
    sub = parser.add_subparsers(dest="command")

    # generate
    gen = sub.add_parser("generate", help="Generate image(s)")
    gen.add_argument("--prompt", required=True, help="Image description")
    gen.add_argument("--negative-prompt", default="", help="What to avoid")
    gen.add_argument("--art-style", default="photorealistic", choices=list(ART_STYLES.keys()))
    gen.add_argument("--quality", default="masterpiece", choices=list(QUALITY_LEVELS.keys()))
    gen.add_argument("--lighting", default="none", choices=list(LIGHTING.keys()))
    gen.add_argument("--atmosphere", default="none", choices=list(ATMOSPHERE.keys()))
    gen.add_argument("--enhancement", default="none", choices=list(ENHANCEMENT.keys()))
    gen.add_argument("--dimensions", default="portrait_hd", choices=list(DIMENSIONS.keys()))
    gen.add_argument("--sampling", default="euler_a", choices=SAMPLING_METHODS)
    gen.add_argument("--model", default="stable-diffusion-xl", choices=AI_MODELS)
    gen.add_argument("--seed", type=int, default=-1)
    gen.add_argument("--guidance", type=float, default=7.0)
    gen.add_argument("--batch", type=int, default=1)
    gen.add_argument("--content-filter", default="none", choices=["none", "pg13"])
    gen.add_argument("--upscale", action="store_true")
    gen.add_argument("--remove-bg", action="store_true")
    gen.add_argument("--enhance-prompt", action="store_true")

    # options
    sub.add_parser("options", help="Print all available options as JSON")

    args = parser.parse_args()

    if args.command == "options":
        print(json.dumps(get_all_options(), indent=2))
        return

    if args.command == "generate":
        opts = DreamForgeOptions(
            prompt=args.prompt,
            negative_prompt=args.negative_prompt,
            art_style=args.art_style,
            quality_level=args.quality,
            lighting=args.lighting,
            atmosphere=args.atmosphere,
            enhancement=args.enhancement,
            dimensions=args.dimensions,
            sampling_method=args.sampling,
            ai_model=args.model,
            seed=args.seed,
            guidance_scale=args.guidance,
            batch_size=args.batch,
            content_filter=args.content_filter,
            upscale=args.upscale,
            remove_background=args.remove_bg,
            prompt_enhancement=args.enhance_prompt,
        )
        result = generate(opts)
        print(json.dumps(result, indent=2))
        return

    parser.print_help()


if __name__ == "__main__":
    main()
