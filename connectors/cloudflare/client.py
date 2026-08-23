"""Cloudflare Workers AI — free image generation via FLUX models.

Free tier: 10,000 neurons/day (~230 images with FLUX Schnell).
No credit card required. Resets daily at 00:00 UTC.

API docs: https://developers.cloudflare.com/workers-ai/
Pricing:  https://developers.cloudflare.com/workers-ai/platform/pricing
"""

from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Models available on Cloudflare Workers AI (text-to-image)
# Neuron costs from: https://developers.cloudflare.com/workers-ai/platform/pricing
# ---------------------------------------------------------------------------
CLOUDFLARE_IMAGE_MODELS: dict[str, dict[str, Any]] = {
    "@cf/black-forest-labs/flux-1-schnell": {
        "label": "FLUX.1 Schnell ⚡",
        "neurons_per_step": 9.60,
        "default_steps": 4,
        "description": "Fast 12B model, ~43 neurons/image",
    },
    "@cf/black-forest-labs/flux-2-dev": {
        "label": "FLUX.2 Dev",
        "neurons_per_step": 9.60,
        "default_steps": 8,
        "description": "High quality, multi-reference support",
    },
    "@cf/black-forest-labs/flux-2-klein-4b": {
        "label": "FLUX.2 Klein 4B",
        "neurons_per_step": 9.60,
        "default_steps": 4,
        "description": "Ultra-fast distilled model",
    },
    "@cf/black-forest-labs/flux-2-klein-9b": {
        "label": "FLUX.2 Klein 9B",
        "neurons_per_step": 9.60,
        "default_steps": 4,
        "description": "Enhanced quality distilled model",
    },
    "@cf/lykon/dreamshaper-8-lcm": {
        "label": "DreamShaper 8 LCM",
        "neurons_per_step": 9.60,
        "default_steps": 4,
        "description": "Photorealistic Stable Diffusion fine-tune",
    },
    "@cf/stabilityai/stable-diffusion-xl-base-1.0": {
        "label": "SDXL Base 1.0",
        "neurons_per_step": 9.60,
        "default_steps": 20,
        "description": "Classic SDXL, higher step count",
    },
}

# Shape → (width, height)
_SHAPES: dict[str, tuple[int, int]] = {
    "square": (1024, 1024),
    "portrait": (768, 1024),
    "landscape": (1024, 768),
}

_OUTPUT_DIR = Path("/home/sifat/sable_output/assets")
_API_BASE = "https://api.cloudflare.com/client/v4"


class CloudflareAIClient:
    """Thin wrapper around Cloudflare Workers AI for image generation."""

    def __init__(self, api_token: str = "", account_id: str = "") -> None:
        self._api_token = api_token.strip()
        self._account_id = account_id.strip()

    @property
    def is_available(self) -> bool:
        return bool(self._api_token)

    def _ensure_account_id(self) -> bool:
        """Auto-fetch account ID from the API if not set. Returns True on success."""
        if self._account_id:
            return True
        if not self._api_token:
            return False
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.get(
                    f"{_API_BASE}/accounts",
                    headers={"Authorization": f"Bearer {self._api_token}"},
                )
                if resp.status_code == 200:
                    accounts = resp.json().get("result", [])
                    if accounts:
                        self._account_id = accounts[0]["id"]
                        logger.info("Auto-fetched Cloudflare account ID: %s", self._account_id[:8])
                        return True
            logger.warning("Could not auto-fetch Cloudflare account ID")
        except Exception:
            logger.exception("Failed to fetch Cloudflare account ID")
        return False

    # ------------------------------------------------------------------
    # Core generation
    # ------------------------------------------------------------------

    def generate_image(
        self,
        prompt: str,
        model: str = "@cf/black-forest-labs/flux-1-schnell",
        shape: str = "square",
        negative_prompt: str = "",
        steps: int | None = None,
        seed: int = -1,
    ) -> dict[str, Any]:
        """Generate an image via Cloudflare Workers AI.

        Returns dict with keys: ok, images, count, provider, errors.
        Each image dict has: url (data URI), width, height, seed, size_bytes.
        """
        if not self.is_available:
            return {"ok": False, "error": "Cloudflare API token not configured"}
        if not self._ensure_account_id():
            return {"ok": False, "error": "Could not fetch Cloudflare account ID from token"}

        meta = CLOUDFLARE_IMAGE_MODELS.get(model)
        if not meta:
            return {"ok": False, "error": f"Unknown model: {model}"}

        width, height = _SHAPES.get(shape, (1024, 1024))
        if steps is None:
            steps = meta["default_steps"]

        # FLUX Schnell only accepts prompt + steps; other models may accept more.
        # We send minimal safe params and let the API reject unsupported ones gracefully.
        payload: dict[str, Any] = {
            "prompt": prompt,
            "steps": steps,
        }
        # Only add optional params for models that support them
        model_id = model.split("/")[-1]
        if "sd" in model_id or "dreamshaper" in model_id:
            payload["width"] = width
            payload["height"] = height
        if negative_prompt and ("sd" in model_id or "dreamshaper" in model_id):
            payload["negative_prompt"] = negative_prompt
        if seed >= 0:
            payload["seed"] = seed

        url = f"{_API_BASE}/accounts/{self._account_id}/ai/run/{model}"
        headers = {
            "Authorization": f"Bearer {self._api_token}",
            "Content-Type": "application/json",
        }

        t0 = time.time()
        try:
            with httpx.Client(timeout=120) as client:
                resp = client.post(url, json=payload, headers=headers)

            elapsed = time.time() - t0

            if resp.status_code != 200:
                err_body = resp.text[:500]
                logger.error("Cloudflare AI error %d: %s", resp.status_code, err_body)
                return {"ok": False, "error": f"HTTP {resp.status_code}: {err_body}"}

            data = resp.json()
            b64_image = data.get("result", {}).get("image", "")
            if not b64_image:
                return {"ok": False, "error": "No image in response"}

            img_bytes = base64.b64decode(b64_image)

            # Save to output dir
            _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            ts = int(time.time() * 1000)
            fname = f"cf_{model.split('/')[-1]}_{ts}.jpg"
            out_path = _OUTPUT_DIR / fname
            out_path.write_bytes(img_bytes)

            actual_seed = data.get("result", {}).get("seed", seed)

            return {
                "ok": True,
                "provider": "cloudflare",
                "count": 1,
                "images": [
                    {
                        "url": f"data:image/jpeg;base64,{b64_image}",
                        "file": str(out_path),
                        "filename": fname,
                        "width": width,
                        "height": height,
                        "seed": actual_seed,
                        "size_bytes": len(img_bytes),
                        "model": model,
                        "steps": steps,
                        "elapsed_s": round(elapsed, 2),
                    }
                ],
            }

        except Exception as exc:
            logger.exception("Cloudflare AI request failed")
            return {"ok": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # Usage estimation (neurons are per-day, not queryable via API)
    # ------------------------------------------------------------------

    def estimate_daily_budget(self, model: str = "@cf/black-forest-labs/flux-1-schnell") -> dict[str, Any]:
        """Estimate how many images can be generated per day with the free tier."""
        meta = CLOUDFLARE_IMAGE_MODELS.get(model)
        if not meta:
            return {"ok": False, "error": f"Unknown model: {model}"}

        free_neurons = 10_000
        neurons_per_image = meta["neurons_per_step"] * meta["default_steps"]
        max_images = int(free_neurons / neurons_per_image) if neurons_per_image > 0 else 0

        return {
            "ok": True,
            "free_neurons_per_day": free_neurons,
            "neurons_per_image": round(neurons_per_image, 1),
            "estimated_images_per_day": max_images,
            "model": model,
        }


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_client: CloudflareAIClient | None = None


def get_client() -> CloudflareAIClient:
    """Return singleton CloudflareAIClient, loading credentials from disk."""
    global _client
    if _client is None:
        creds_file = Path("/home/sifat/hdd/projects/Sable/system/.cloudflare_ai_creds.json")
        token = ""
        account_id = ""
        if creds_file.exists():
            try:
                data = json.loads(creds_file.read_text())
                token = data.get("api_token", "")
                account_id = data.get("account_id", "")
            except Exception:
                pass
        _client = CloudflareAIClient(api_token=token, account_id=account_id)
    return _client


def save_credentials(api_token: str, account_id: str = "") -> None:
    """Persist credentials and reset singleton. Account ID is auto-fetched if omitted."""
    global _client
    creds_file = Path("/home/sifat/hdd/projects/Sable/system/.cloudflare_ai_creds.json")
    creds_file.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, str] = {"api_token": api_token}
    if account_id:
        data["account_id"] = account_id
    creds_file.write_text(json.dumps(data, indent=2))
    _client = CloudflareAIClient(api_token=api_token, account_id=account_id)
