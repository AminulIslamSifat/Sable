"""Image Generator handler: Cloudflare → Pollinations → Perchance fallback chain."""

from __future__ import annotations

import json
import logging
import subprocess
import time
import urllib.parse
import urllib.request
from collections.abc import Generator
from pathlib import Path
from typing import Any

from engine.skills.handlers.common import _end_event, _output_event

logger = logging.getLogger(__name__)

_SCRIPT = str(Path(__file__).resolve().parent.parent.parent.parent / "tools" / "image_generator" / "scripts" / "image_generator.py")
_OUTPUT_DIR = Path("/home/sifat/sable_output/assets")

# Shape → (width, height) mappings
_SHAPES = {
    "square": (1024, 1024),
    "portrait": (768, 1024),
    "landscape": (1024, 768),
}

# Style prefixes (for providers that don't have built-in styles)
_STYLE_PREFIXES = {
    "ghibli": "Studio Ghibli art style, Hayao Miyazaki inspired, soft watercolor anime, whimsical",
    "anime": "anime art style, high quality anime illustration, detailed anime artwork",
    "cyberpunk": "cyberpunk art style, neon lights, dark futuristic city, holographic",
    "photorealistic": "photorealistic, hyperrealistic, 8k uhd, dslr photo, sharp focus",
    "oil_painting": "oil painting, classical art style, rich textures, dramatic lighting",
    "digital_art": "digital art, concept art, highly detailed, professional illustration",
    "pixel_art": "pixel art style, retro game aesthetic, 16-bit, crisp pixels",
    "watercolor": "watercolor painting, soft washes, delicate colors, artistic",
    "cinematic": "cinematic shot, movie still, dramatic lighting, film grain, color graded",
    "fantasy": "fantasy art, epic scale, magical atmosphere, detailed illustration",
    "sci_fi": "science fiction art, futuristic, sleek design, advanced technology",
}


def _apply_style(prompt: str, style: str) -> str:
    """Prepend style prefix to prompt if applicable."""
    prefix = _STYLE_PREFIXES.get(style, "")
    return f"{prefix}, {prompt}" if prefix else prompt


def _try_cloudflare(prompt: str, model: str, shape: str, negative_prompt: str, seed: int) -> dict | None:
    """Attempt Cloudflare Workers AI generation. Returns result dict or None on failure."""
    try:
        from connectors.cloudflare.client import get_client
        client = get_client()
        if not client.is_available:
            logger.debug("Cloudflare: not configured, skipping")
            return None

        result = client.generate_image(
            prompt=prompt,
            model=model,
            shape=shape,
            negative_prompt=negative_prompt,
            seed=seed,
        )
        if result.get("ok"):
            result["provider"] = "cloudflare"
            return result
        logger.warning("Cloudflare failed: %s", result.get("error", "unknown"))
        return None
    except Exception as e:
        logger.warning("Cloudflare exception: %s", e)
        return None


def _try_pollinations(prompt: str, model: str, shape: str, seed: int) -> dict | None:
    """Attempt Pollinations generation. Returns result dict or None on failure."""
    try:
        w, h = _SHAPES.get(shape, (1024, 1024))
        encoded = urllib.parse.quote(prompt)
        url = f"https://image.pollinations.ai/prompt/{encoded}?width={w}&height={h}&model={model}&nologo=true"
        if seed >= 0:
            url += f"&seed={seed}"

        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        fname = f"gen_poll_{int(time.time())}_{seed if seed >= 0 else 'rand'}.jpg"
        out_path = _OUTPUT_DIR / fname

        req = urllib.request.Request(url, headers={"User-Agent": "Sable/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()

        if len(data) < 5000:
            logger.warning("Pollinations: response too small (%d bytes)", len(data))
            return None

        out_path.write_bytes(data)
        return {
            "ok": True,
            "provider": "pollinations",
            "images": [{
                "ok": True,
                "path": str(out_path),
                "filename": fname,
                "seed": seed if seed >= 0 else 0,
                "width": w,
                "height": h,
                "size_bytes": len(data),
            }],
            "count": 1,
            "model": model,
            "shape": shape,
            "prompt_used": prompt,
        }
    except Exception as e:
        logger.warning("Pollinations failed: %s", e)
        return None


def _try_puter(prompt: str, model: str, shape: str, neg: str, count: int) -> dict | None:
    """Attempt Puter image generation. Returns result dict or None on failure."""
    try:
        from connectors.puter.client import get_client
        client = get_client()
        if not client.is_available:
            logger.debug("Puter: not configured, skipping")
            return None

        result = client.generate_image(
            prompt=prompt,
            model=model,
            shape=shape,
            negative_prompt=neg,
            count=count,
        )
        if result.get("ok"):
            result["provider"] = "puter"
            return result
        logger.warning("Puter failed: %s", result.get("error", "unknown"))
        return None
    except Exception as e:
        logger.warning("Puter exception: %s", e)
        return None


def _try_perchance(prompt: str, style: str, shape: str, count: str, neg: str, seed: str) -> dict | None:
    """Fallback to Perchance CLI script. Returns parsed result dict or None."""
    cli = ["generate", "--prompt", prompt, "--style", style, "--shape", shape, "--count", count]
    if neg:
        cli += ["--negative-prompt", neg]
    if seed and seed != "-1":
        cli += ["--seed", seed]

    cmd = ["python3", _SCRIPT] + cli
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        logger.warning("Perchance: timed out")
        return None
    except Exception as e:
        logger.warning("Perchance exception: %s", e)
        return None

    output = proc.stdout.strip()
    if proc.returncode != 0:
        logger.warning("Perchance failed: %s", proc.stderr.strip() or output)
        return None

    try:
        result = json.loads(output)
        if result.get("ok"):
            result["provider"] = "perchance"
            return result
    except (json.JSONDecodeError, TypeError):
        pass
    return None


def handle_generate_image(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()
    prompt = attrs.get("prompt", content.strip() if content else "")

    if not prompt:
        yield _output_event(tag_id, "Error: prompt is required\n")
        yield _end_event(tag_id, name, False, started, {"error": "missing prompt"})
        return

    style = attrs.get("style", "no_style")
    shape = attrs.get("shape", "square")
    count = attrs.get("count", "1")
    neg = attrs.get("negative_prompt", "")
    seed_str = attrs.get("seed", "-1")
    model = attrs.get("model", "@cf/black-forest-labs/flux-1-schnell")
    provider_pref = attrs.get("provider", "auto")

    try:
        seed = int(seed_str)
    except (ValueError, TypeError):
        seed = -1

    # Apply style prefix for non-Perchance providers
    full_prompt = _apply_style(prompt, style) if style != "no_style" else prompt

    try:
        num_images = max(1, min(int(count), 4))
    except (ValueError, TypeError):
        num_images = 1

    yield _output_event(tag_id, f"🎨 Generating {num_images} image(s)… (provider: {provider_pref})\n", "command")

    # Perchance handles batch natively; other providers need a loop
    if provider_pref == "perchance" or (provider_pref == "auto" and num_images > 1):
        # For auto with count>1, we still try single-image providers first,
        # but Perchance gets the native batch call
        pass

    result: dict | None = None
    tried: list[str] = []

    if num_images == 1:
        # Single image path — same as before
        if provider_pref == "auto":
            for msg in ["⏳ Trying Cloudflare AI…\n"]:
                yield _output_event(tag_id, msg)
            result = _try_cloudflare(full_prompt, model, shape, neg, seed)
            if result:
                tried.append("cloudflare ✅")
            else:
                tried.append("cloudflare ❌")
                yield _output_event(tag_id, "⏳ Cloudflare unavailable, trying Pollinations…\n")
                poll_model = "flux" if "flux" in model.lower() else "turbo"
                result = _try_pollinations(full_prompt, poll_model, shape, seed)
                if result:
                    tried.append("pollinations ✅")
                else:
                    tried.append("pollinations ❌")
                    yield _output_event(tag_id, "⏳ Pollinations failed, falling back to Perchance…\n")
                    result = _try_perchance(prompt, style, shape, count, neg, seed_str)
                    if result:
                        tried.append("perchance ✅")
                    else:
                        tried.append("perchance ❌")
        elif provider_pref == "cloudflare":
            result = _try_cloudflare(full_prompt, model, shape, neg, seed)
            tried.append("cloudflare" + (" ✅" if result else " ❌"))
        elif provider_pref == "pollinations":
            poll_model = attrs.get("model", "flux")
            result = _try_pollinations(full_prompt, poll_model, shape, seed)
            tried.append("pollinations" + (" ✅" if result else " ❌"))
        elif provider_pref == "puter":
            puter_model = attrs.get("model", "openai/gpt-image-1-mini")
            result = _try_puter(full_prompt, puter_model, shape, neg, num_images)
            tried.append("puter" + (" ✅" if result else " ❌"))
        elif provider_pref == "perchance":
            result = _try_perchance(prompt, style, shape, count, neg, seed_str)
            tried.append("perchance" + (" ✅" if result else " ❌"))
    else:
        # Multi-image path — loop for non-Perchance, native batch for Perchance
        if provider_pref == "perchance":
            result = _try_perchance(prompt, style, shape, str(num_images), neg, seed_str)
            tried.append("perchance" + (" ✅" if result else " ❌"))
        elif provider_pref == "cloudflare":
            all_images: list[dict] = []
            for i in range(num_images):
                img_seed = seed if (i == 0 and seed >= 0) else -1
                r = _try_cloudflare(full_prompt, model, shape, neg, img_seed)
                if r and r.get("images"):
                    all_images.extend(r["images"])
                    yield _output_event(tag_id, f"  ✅ Image {i+1}/{num_images}\n")
                else:
                    yield _output_event(tag_id, f"  ❌ Image {i+1}/{num_images} failed\n")
            if all_images:
                result = {"ok": True, "images": all_images, "count": len(all_images), "provider": "cloudflare"}
            tried.append("cloudflare" + (" ✅" if all_images else " ❌"))
        elif provider_pref == "pollinations":
            poll_model = attrs.get("model", "flux")
            all_images = []
            for i in range(num_images):
                img_seed = seed if (i == 0 and seed >= 0) else -1
                r = _try_pollinations(full_prompt, poll_model, shape, img_seed)
                if r and r.get("images"):
                    all_images.extend(r["images"])
                    yield _output_event(tag_id, f"  ✅ Image {i+1}/{num_images}\n")
                else:
                    yield _output_event(tag_id, f"  ❌ Image {i+1}/{num_images} failed\n")
            if all_images:
                result = {"ok": True, "images": all_images, "count": len(all_images), "provider": "pollinations"}
            tried.append("pollinations" + (" ✅" if all_images else " ❌"))
        elif provider_pref == "puter":
            # Puter handles batch natively
            puter_model = attrs.get("model", "openai/gpt-image-1-mini")
            result = _try_puter(full_prompt, puter_model, shape, neg, num_images)
            if result and result.get("images"):
                for i in range(len(result["images"])):
                    yield _output_event(tag_id, f"  ✅ Image {i+1}/{num_images}\n")
            tried.append("puter" + (" ✅" if result else " ❌"))
        elif provider_pref == "auto":
            # Auto with multi: try Cloudflare first (looped), then Pollinations, then Perchance batch
            all_images = []
            for i in range(num_images):
                img_seed = seed if (i == 0 and seed >= 0) else -1
                r = _try_cloudflare(full_prompt, model, shape, neg, img_seed)
                if r and r.get("images"):
                    all_images.extend(r["images"])
                    yield _output_event(tag_id, f"  ✅ Image {i+1}/{num_images} (cloudflare)\n")
                else:
                    break
            if len(all_images) == num_images:
                tried.append("cloudflare ✅")
                result = {"ok": True, "images": all_images, "count": len(all_images), "provider": "cloudflare"}
            else:
                # Fall back to Perchance batch for remaining
                remaining = num_images - len(all_images)
                yield _output_event(tag_id, f"⏳ Cloudflare partial ({len(all_images)}/{num_images}), trying Perchance for rest…\n")
                r = _try_perchance(prompt, style, shape, str(remaining), neg, seed_str)
                if r and r.get("images"):
                    all_images.extend(r["images"])
                    tried.append(f"cloudflare({len(all_images)-remaining}) → perchance({remaining}) ✅")
                    result = {"ok": True, "images": all_images, "count": len(all_images), "provider": "mixed"}
                else:
                    tried.append("cloudflare ❌ → perchance ❌")

    # Build concise output — no JSON dumps, just status + path or error
    chain_info = " → ".join(tried)
    ok = result is not None and result.get("ok", False)

    result_meta: dict[str, Any] = {
        "action": "generate",
        "style": style,
        "shape": shape,
        "chain": chain_info,
    }

    if ok and result:
        actual_provider = result.get("provider", "unknown")
        images_list = result.get("images") or [{}]
        total_count = len(images_list)
        first = images_list[0]
        filename = first.get("filename", "")
        img_path = first.get("path", first.get("file", ""))
        width = first.get("width", 0)
        height = first.get("height", 0)
        seed_val = first.get("seed", "?")

        # Concise output for the model
        yield _output_event(tag_id, f"✅ Generated {total_count} image(s) via {actual_provider}\n")
        for idx, img in enumerate(images_list):
            fn = img.get("filename", "")
            ip = img.get("path", img.get("file", ""))
            w = img.get("width", 0)
            h = img.get("height", 0)
            s = img.get("seed", "?")
            yield _output_event(tag_id, f"  [{idx+1}] {ip} ({w}x{h}, seed={s})\n")

        # Build images array for frontend gallery
        images_meta = []
        for img in images_list:
            fn = img.get("filename", "")
            images_meta.append({
                "url": f"/assets/{fn}" if fn else "",
                "mime": "image/jpeg",
                "filename": fn,
                "path": img.get("path", img.get("file", "")),
                "seed": img.get("seed", "?"),
                "width": img.get("width", 0),
                "height": img.get("height", 0),
            })

        # Primary image (first) for backward compat
        result_meta["kind"] = "image"
        result_meta["url"] = f"/assets/{filename}" if filename else ""
        result_meta["mime"] = "image/jpeg"
        result_meta["filename"] = filename
        result_meta["path"] = img_path
        result_meta["seed"] = seed_val
        result_meta["width"] = width
        result_meta["height"] = height
        result_meta["provider"] = actual_provider
        result_meta["count"] = total_count
        # Full images array for gallery rendering
        if total_count > 1:
            result_meta["images"] = images_meta
    else:
        error_msg = result.get("error", "All providers failed") if result else "All providers failed"
        yield _output_event(tag_id, f"❌ Generation failed: {error_msg}\n")
        yield _output_event(tag_id, f"Chain: {chain_info}\n")
        result_meta["error"] = error_msg

    yield _end_event(tag_id, name, ok, started, result_meta)
