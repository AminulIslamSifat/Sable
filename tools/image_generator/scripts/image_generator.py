#!/usr/bin/env python3
"""Perchance AI Image Generator CLI — standalone tool for Sable.

Usage:
    python3 image_generator.py generate --prompt "..." [--style ghibli] [--shape square] [--count 1]
    python3 image_generator.py list-styles
    python3 image_generator.py styles-json

Can also be imported as a module:
    from tools.image_generator.image_generator import generate_image, STYLES, SHAPES
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from pathlib import Path

try:
    import httpx
except ImportError:
    print("Error: httpx not installed. Run: pip install httpx", file=sys.stderr)
    sys.exit(1)

import sys as _sys
if str(Path(__file__).resolve().parent) not in _sys.path:
    _sys.path.insert(0, str(Path(__file__).resolve().parent))

from perchance_key import (
    get_valid_key as _shared_get_valid_key,
    save_key as _shared_save_key,
    load_cached_key,
    verify_key as _shared_verify_key,
)

# ─── Constants ─────────────────────────────────────────────────────────────────────────────────────......

BASE = "https://image-generation.perchance.org/api"
AD_ACCESS_CODE = "a4c88828629d9d1e2c98fa76fd2b5eccb69ee18af472e567febff4b943e9bbd6"
KEY_CACHE_FILE = Path(__file__).resolve().parent.parent.parent.parent / "system" / ".perchance_key"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "Referer": "https://image-generation.perchance.org/embed",
    "Content-Type": "text/plain;charset=UTF-8",
}

OUTPUT_DIR = Path.home() / "sable_output" / "assets"


# ─── Key Management (delegated to perchance_key module) ───────────────────────────────────────────────────────────────────────────────────......

def _verify_key(key: str) -> bool:
    """Check if a Perchance userKey is still valid."""
    return _shared_verify_key(key)


def save_key(key: str) -> None:
    """Save a key to cache file."""
    _shared_save_key(key, KEY_CACHE_FILE)


def _load_cached_key() -> str | None:
    """Load key from cache file."""
    return load_cached_key(KEY_CACHE_FILE)


def get_valid_key(override_key: str | None = None) -> str:
    """Get a valid Perchance userKey via the shared perchance_key module.

    Priority: override_key > PERCHANCE_KEY env > cached file > browser refresh.
    Raw httpx calls to verifyUser are intentionally not used because Perchance
    requires a browser-solved Cloudflare Turnstile token.
    """
    return _shared_get_valid_key(
        KEY_CACHE_FILE,
        override_key=override_key,
        env_var="PERCHANCE_KEY",
        tag="perchance",
    )

SHAPES = {
    "square": "768x768",
    "portrait": "512x768",
    "landscape": "768x512",
}

STYLES = {
    # Anime / Manga
    "painted_anime": "painterly anime artwork, world-class masterpiece, fine details, breathtaking artwork, painterly art style, high quality, 8k, very detailed, high resolution, exquisite composition and lighting",
    "anime": "anime art style, high quality anime illustration, detailed anime artwork",
    "ghibli": "Studio Ghibli art style, Hayao Miyazaki inspired, soft watercolor anime, whimsical, beautiful scenery",
    "your_name": "Your Name anime art style, Makoto Shinkai inspired, breathtaking sky, lens flare, vivid colors",
    "wlop": "inspired by wlop art style, digital painting, ethereal lighting, semi-realistic anime",
    "atey_ghailan": "art in the style of atey ghailan, painterly anime style, pixiv masterpiece",
    "kantoku": "in the style of kantoku, soft pastel anime, delicate linework, beautiful eyes",
    "redjuice": "in the style of redjuice, mechanical anime art, detailed sci-fi design",
    # Painting / Traditional
    "oil_painting": "oil painting, classical art style, rich textures, dramatic lighting, museum quality",
    "watercolor": "watercolor painting, soft washes, delicate colors, artistic, paper texture",
    "acrylic": "acrylic painting, bold brushstrokes, vibrant colors, textured canvas",
    "impressionist": "impressionist painting style, Monet inspired, soft brushstrokes, light and color",
    "ukiyo_e": "ukiyo-e art style, traditional Japanese woodblock print, flat colors, bold outlines",
    "art_nouveau": "Art Nouveau style, Mucha inspired, ornamental, flowing organic lines, elegant",
    "renaissance": "Renaissance painting style, classical composition, chiaroscuro, oil on canvas",
    # Digital / Modern
    "digital_art": "digital art, concept art, highly detailed, professional illustration",
    "concept_art": "concept art, matte painting, cinematic, epic scale, professional game art",
    "pixel_art": "pixel art style, retro game aesthetic, 16-bit, crisp pixels, limited palette",
    "comic_book": "comic book art style, bold ink lines, halftone dots, dynamic poses, Marvel/DC style",
    "manga": "manga art style, black and white, screentones, dynamic action lines",
    "cartoon": "cartoon style, clean lines, bright colors, expressive characters, Disney/Pixar inspired",
    # Photography / Realistic
    "photorealistic": "photorealistic, hyperrealistic, 8k uhd, dslr photo, sharp focus, professional photography",
    "cinematic": "cinematic shot, movie still, dramatic lighting, anamorphic lens, film grain, color graded",
    "portrait": "professional portrait photography, studio lighting, shallow depth of field, bokeh background",
    # Stylized / Artistic
    "cyberpunk": "cyberpunk art style, neon lights, dark futuristic city, rain-slicked streets, holographic",
    "steampunk": "steampunk art style, brass gears, Victorian era technology, steam-powered, ornate machinery",
    "vaporwave": "vaporwave aesthetic, retro 80s/90s, neon pink and purple, glitch art, nostalgic",
    "fantasy": "epic fantasy art, magical, dragons and castles, D&D inspired, dramatic lighting",
    "sci_fi": "science fiction art, space opera, advanced technology, alien worlds, cosmic",
    "horror": "dark horror art, eerie atmosphere, Lovecraftian, unsettling, shadowy",
    "surreal": "surrealist art style, Dali inspired, dreamlike, impossible geometry, melting forms",
    "pop_art": "pop art style, Warhol inspired, bold colors, commercial art aesthetic, screenprint",
    "low_poly": "low poly 3D art style, geometric, faceted surfaces, minimalist, clean",
    "isometric": "isometric art style, 3/4 view, clean vector, game asset aesthetic",
    "mtg_card": "Magic the Gathering card art, incredible fantasy artwork, epic composition",
    "50s_enamel": "50s enamel sign art, vintage advertisement, retro Americana, bold typography",
    "no_style": "",
}

DEFAULT_NEGATIVE = "low-quality, deformed, blurry, bad art, extra fingers, mutated hands"


# ─── Core API ────────────────────────────────────────────────────────────────

def _generate_single(full_prompt: str, neg: str, resolution: str, seed: int, user_key: str | None = None) -> dict:
    """Generate a single image via Perchance API. Returns raw result dict."""
    if user_key is None:
        user_key = get_valid_key()

    body = {
        "prompt": full_prompt,
        "negativePrompt": neg,
        "resolution": resolution,
        "guidanceScale": 7,
        "numInferenceSteps": 30,
        "seed": seed,
        "numImages": 1,
    }

    request_id = str(random.random())
    cache_bust = str(random.random())
    url = (
        f"{BASE}/generate"
        f"?userKey={user_key}"
        f"&requestId={request_id}"
        f"&adAccessCode={AD_ACCESS_CODE}"
        f"&__cacheBust={cache_bust}"
    )

    try:
        resp = httpx.post(url, content=json.dumps(body), headers=HEADERS, timeout=360)
    except Exception as e:
        return {"ok": False, "error": f"Request failed: {e}"}

    if resp.status_code != 200:
        return {"ok": False, "error": f"API returned {resp.status_code}: {resp.text[:300]}"}

    data = resp.json()

    # Auto-refresh key if invalid
    if data.get("status") == "invalid_key":
        print(f"[perchance] API returned invalid_key, waiting 3s for propagation then retrying...", file=sys.stderr)
        time.sleep(3)
        # First retry with same key (might just need propagation time)
        cache_bust_retry = str(random.random())
        url_retry = (
            f"{BASE}/generate"
            f"?userKey={user_key}"
            f"&requestId={str(random.random())}"
            f"&adAccessCode={AD_ACCESS_CODE}"
            f"&__cacheBust={cache_bust_retry}"
        )
        propagation_ok = False
        try:
            resp_retry = httpx.post(url_retry, content=json.dumps(body), headers=HEADERS, timeout=360)
            if resp_retry.status_code == 200:
                data_retry = resp_retry.json()
                if data_retry.get("status") != "invalid_key":
                    print("[perchance] Retry with same key succeeded after propagation delay", file=sys.stderr)
                    data = data_retry
                    propagation_ok = True
                else:
                    print("[perchance] Same key still invalid after propagation delay", file=sys.stderr)
            else:
                print(f"[perchance] Propagation retry got status {resp_retry.status_code}", file=sys.stderr)
        except Exception as e:
            print(f"[perchance] Propagation retry failed: {e}", file=sys.stderr)

        if not propagation_ok:
            # Still invalid — invalidate cache and get fresh key
            print("[perchance] Fetching new key via browser...", file=sys.stderr)
            if KEY_CACHE_FILE.exists():
                KEY_CACHE_FILE.unlink()
            try:
                user_key = get_valid_key()
            except RuntimeError as e:
                return {"ok": False, "error": str(e)}
            # Retry with fresh key
            cache_bust = str(random.random())
            url = (
                f"{BASE}/generate"
                f"?userKey={user_key}"
                f"&requestId={str(random.random())}"
                f"&adAccessCode={AD_ACCESS_CODE}"
                f"&__cacheBust={cache_bust}"
            )
            try:
                resp = httpx.post(url, content=json.dumps(body), headers=HEADERS, timeout=360)
                data = resp.json()
            except Exception as e:
                return {"ok": False, "error": f"Retry failed: {e}"}

    if data.get("status") != "success":
        return {"ok": False, "error": f"Generation failed: {json.dumps(data)[:300]}"}

    download_path = data.get("imageDownloadUrl", "")
    if not download_path:
        return {"ok": False, "error": "No download URL in response"}

    full_url = f"https://image-generation.perchance.org{download_path}"
    try:
        img_resp = httpx.get(full_url, headers=HEADERS, timeout=90, follow_redirects=True)
    except Exception as e:
        return {"ok": False, "error": f"Download failed: {e}"}

    if img_resp.status_code != 200 or len(img_resp.content) < 5000:
        return {"ok": False, "error": f"Download issue: status={img_resp.status_code}, size={len(img_resp.content)}"}

    ts = int(time.time())
    img_seed = data.get("seed", "unknown")
    filename = f"gen_{ts}_{img_seed}.jpg"
    out_path = OUTPUT_DIR / filename
    out_path.write_bytes(img_resp.content)

    return {
        "ok": True,
        "path": str(out_path),
        "filename": filename,
        "seed": img_seed,
        "width": data.get("width"),
        "height": data.get("height"),
        "size_bytes": len(img_resp.content),
    }


def generate_image(
    prompt: str,
    style: str = "no_style",
    shape: str = "square",
    count: int = 1,
    negative_prompt: str = "",
    seed: int = -1,
    key: str | None = None,
) -> dict:
    """Generate image(s) via Perchance API. Returns result dict with images array."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        user_key = get_valid_key(key)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}

    style_prefix = STYLES.get(style, "")
    full_prompt = f"{style_prefix}, {prompt}" if style_prefix else prompt
    resolution = SHAPES.get(shape, "768x768")
    neg = negative_prompt or DEFAULT_NEGATIVE
    count = max(1, min(count, 4))

    images = []
    errors = []
    for i in range(count):
        # Use provided seed for first image, random for rest
        img_seed = seed if (i == 0 and seed != -1) else -1
        result = _generate_single(full_prompt, neg, resolution, img_seed, user_key)
        if result.get("ok"):
            images.append(result)
        else:
            errors.append(f"Image {i+1}: {result.get('error', 'unknown')}")

    if not images:
        return {"ok": False, "error": "; ".join(errors) or "All generations failed"}

    return {
        "ok": True,
        "images": images,
        "count": len(images),
        "style": style,
        "shape": shape,
        "prompt_used": full_prompt,
        "errors": errors if errors else None,
    }


# ─── CLI ─────────────────────────────────────────────────────────────────────

def cmd_generate(args: argparse.Namespace) -> None:
    result = generate_image(
        prompt=args.prompt,
        style=args.style or "no_style",
        shape=args.shape or "square",
        count=args.count or 1,
        negative_prompt=args.negative_prompt or "",
        seed=args.seed if args.seed is not None else -1,
        key=getattr(args, "key", None),
    )
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("ok") else 1)


def cmd_save_key(args: argparse.Namespace) -> None:
    key = args.key.strip()
    if len(key) != 64:
        print(json.dumps({"ok": False, "error": f"Key must be 64 hex chars, got {len(key)}"}))
        sys.exit(1)
    if _verify_key(key):
        save_key(key)
        print(json.dumps({"ok": True, "message": "Key saved and verified"}))
    else:
        print(json.dumps({"ok": False, "error": "Key is invalid or expired"}))
        sys.exit(1)


def cmd_verify_key(args: argparse.Namespace) -> None:
    key = args.key.strip() if args.key else None
    try:
        valid_key = get_valid_key(key)
        print(json.dumps({"ok": True, "valid": True, "key": valid_key[:12] + "..."}))
    except RuntimeError as e:
        print(json.dumps({"ok": True, "valid": False, "error": str(e)}))


def cmd_list_styles(_args: argparse.Namespace) -> None:
    for name, prefix in STYLES.items():
        desc = prefix[:80] + "..." if len(prefix) > 80 else prefix
        print(f"  {name:20s} → {desc or '(raw prompt, no prefix)'}")


def cmd_styles_json(_args: argparse.Namespace) -> None:
    print(json.dumps(STYLES, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Perchance AI Image Generator")
    sub = parser.add_subparsers(dest="command")

    gen = sub.add_parser("generate", help="Generate an image")
    gen.add_argument("--prompt", required=True, help="Image description")
    gen.add_argument("--style", default="no_style", help="Style preset key")
    gen.add_argument("--shape", default="square", choices=list(SHAPES.keys()))
    gen.add_argument("--count", type=int, default=1, help="Number of images (1-4)")
    gen.add_argument("--negative-prompt", default="", help="Negative prompt")
    gen.add_argument("--seed", type=int, default=-1, help="Seed (-1 for random)")
    gen.add_argument("--key", default=None, help="Perchance userKey override")
    gen.set_defaults(func=cmd_generate)

    sk = sub.add_parser("save-key", help="Save and verify a Perchance userKey")
    sk.add_argument("key", help="64-char hex userKey")
    sk.set_defaults(func=cmd_save_key)

    vk = sub.add_parser("verify-key", help="Verify a Perchance userKey")
    vk.add_argument("key", nargs="?", default=None, help="Key to verify (default: check cached)")
    vk.set_defaults(func=cmd_verify_key)

    ls = sub.add_parser("list-styles", help="List available styles")
    ls.set_defaults(func=cmd_list_styles)

    sj = sub.add_parser("styles-json", help="Output styles as JSON")
    sj.set_defaults(func=cmd_styles_json)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
