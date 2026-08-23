"""Puter.com API connector for Sable — free image generation via driver endpoint.

Uses the raw /drivers/call endpoint (free tier works here; the OpenAI-compat
wrapper requires a paid subscription). Each Puter account gets a monthly
allowance (~1000 credits); image gen costs ~4.4 credits each.

Multi-key rotation, persisted to system/.puter_api_keys.json.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("sable.puter")

_SYSTEM_DIR = Path(__file__).resolve().parent.parent.parent / "system"
_KEYS_PATH = _SYSTEM_DIR / ".puter_api_keys.json"
_DRIVER_URL = "https://api.puter.com/drivers/call"
_USAGE_URL = "https://api.puter.com/metering/usage"
_OUTPUT_DIR = Path("/home/sifat/sable_output/assets")

# Models verified working on the FREE tier (probed 2026-08-17).
# Other models exist but require paid credits ("Insufficient credits").
# This list is what actually works without topping up.
PUTER_IMAGE_MODELS: dict[str, str] = {
    # OpenAI (all 4 confirmed ✅)
    "openai/gpt-image-2": "GPT Image 2",
    "openai/gpt-image-1.5": "GPT Image 1.5",
    "openai/gpt-image-1-mini": "GPT Image 1 Mini ⚡",
    "openai/gpt-image-1": "GPT Image 1",
    # Google (all 6 confirmed ✅)
    "google/gemini-3-pro-image-preview": "Gemini 3 Pro Image",
    "google/gemini-3.1-flash-image-preview": "Gemini 3.1 Flash Image",
    "google/gemini-2.5-flash-image": "Gemini 2.5 Flash Image",
    "google/imagen-4.0-ultra": "Imagen 4 Ultra",
    "google/imagen-4.0-fast": "Imagen 4 Fast",
    "google/imagen-4.0": "Imagen 4",
}

# Shape → (width, height) for the generate call
_SHAPES: dict[str, tuple[int, int]] = {
    "square": (1024, 1024),
    "portrait": (768, 1024),
    "landscape": (1024, 768),
}


# ---------------------------------------------------------------------------
# Key persistence
# ---------------------------------------------------------------------------

def _load_keys() -> list[str]:
    if _KEYS_PATH.exists():
        try:
            data = json.loads(_KEYS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [k for k in data if isinstance(k, str) and k.strip()]
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _save_keys(keys: list[str]) -> None:
    _SYSTEM_DIR.mkdir(parents=True, exist_ok=True)
    _KEYS_PATH.write_text(json.dumps(keys, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class PuterClient:
    """Puter driver-API client with multi-key rotation for image generation."""

    def __init__(self) -> None:
        self._keys: list[str] = _load_keys()
        self._key_index: int = 0

    # -- key management -----------------------------------------------------

    @property
    def is_available(self) -> bool:
        return len(self._keys) > 0

    @property
    def _current_key(self) -> str | None:
        if not self._keys:
            return None
        return self._keys[self._key_index % len(self._keys)]

    def add_key(self, key: str) -> None:
        key = key.strip()
        if key and key not in self._keys:
            self._keys.append(key)
            _save_keys(self._keys)

    def remove_key(self, index: int) -> bool:
        if 0 <= index < len(self._keys):
            self._keys.pop(index)
            if self._key_index >= len(self._keys) and self._keys:
                self._key_index = 0
            _save_keys(self._keys)
            return True
        return False

    def list_keys(self) -> list[dict[str, Any]]:
        result = []
        for i, key in enumerate(self._keys):
            masked = key[:8] + "..." + key[-4:] if len(key) > 12 else "***"
            result.append({
                "index": i,
                "masked": masked,
                "active": i == (self._key_index % len(self._keys)) if self._keys else False,
            })
        return result

    def _rotate(self) -> str | None:
        if len(self._keys) <= 1:
            return self._current_key
        self._key_index = (self._key_index + 1) % len(self._keys)
        return self._keys[self._key_index]

    # -- low-level driver call ---------------------------------------------

    def _driver_call(
        self, interface: str, driver: str, method: str, args: dict[str, Any], timeout: int = 120
    ) -> dict[str, Any]:
        """Call the Puter driver endpoint. Tries each key once on auth/transport failure."""
        last_err: str = "no keys configured"
        attempts = max(1, len(self._keys))
        for _ in range(attempts):
            key = self._current_key
            if not key:
                break
            # Sanitize key: replace Unicode quotes/dashes that sneak in from copy-paste
            safe_key = key.encode("ascii", "ignore").decode("ascii")
            headers = {"Authorization": f"Bearer {safe_key}", "Content-Type": "application/json"}
            payload = {"interface": interface, "driver": driver, "method": method, "args": args}
            try:
                resp = httpx.post(_DRIVER_URL, headers=headers, json=payload, timeout=timeout)
            except httpx.HTTPError as e:
                last_err = f"network: {e}"
                self._rotate()
                continue

            try:
                data = resp.json()
            except Exception:
                last_err = f"non-json response (status {resp.status_code})"
                self._rotate()
                continue

            if data.get("success"):
                return data

            err = data.get("error") or data.get("message") or "unknown error"
            code = data.get("code", "")
            # Auth / token issues → rotate to next key
            if code in ("unauthorized", "forbidden", "invalid_token") or resp.status_code in (401, 403):
                last_err = str(err)[:120]
                self._rotate()
                continue
            # Hard upstream failure — no point rotating
            return {"success": False, "error": str(err)[:300], "code": code}

        return {"success": False, "error": last_err}

    # -- image generation ---------------------------------------------------

    def generate_image(
        self,
        prompt: str,
        model: str = "openai/gpt-image-1-mini",
        shape: str = "square",
        negative_prompt: str = "",
        count: int = 1,
    ) -> dict[str, Any]:
        """Generate image(s). Returns {ok, images:[{path,filename,width,height,size_bytes}], ...}."""
        if not self.is_available:
            return {"ok": False, "error": "No Puter API key configured. Add one in Settings → Providers."}

        w, h = _SHAPES.get(shape, (1024, 1024))
        full_prompt = prompt
        if negative_prompt:
            full_prompt = f"{prompt}. Avoid: {negative_prompt}"

        count = max(1, min(count, 4))
        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        images: list[dict[str, Any]] = []
        errors: list[str] = []

        for i in range(count):
            data = self._driver_call(
                "puter-image-generation", "ai-image", "generate",
                {"prompt": full_prompt, "model": model, "width": w, "height": h},
                timeout=150,
            )
            if not data.get("success"):
                errors.append(f"Image {i+1}: {data.get('error', 'unknown')}")
                continue

            result = data.get("result", "")
            if not isinstance(result, str) or "," not in result:
                errors.append(f"Image {i+1}: unexpected response format")
                continue

            try:
                b64 = result.split(",", 1)[1]
                img_bytes = base64.b64decode(b64)
            except Exception as e:
                errors.append(f"Image {i+1}: decode failed ({e})")
                continue

            ts = int(time.time())
            slug = model.replace("/", "_").replace(" ", "")
            filename = f"gen_puter_{slug}_{ts}_{i}.png"
            out_path = _OUTPUT_DIR / filename
            out_path.write_bytes(img_bytes)
            images.append({
                "ok": True,
                "path": str(out_path),
                "filename": filename,
                "seed": 0,
                "width": w,
                "height": h,
                "size_bytes": len(img_bytes),
            })

        if not images:
            return {"ok": False, "error": "; ".join(errors) or "All generations failed"}

        return {
            "ok": True,
            "images": images,
            "count": len(images),
            "provider": "puter",
            "model": model,
            "shape": shape,
            "prompt_used": prompt,
            "errors": errors if errors else None,
        }

    # -- usage / allowance --------------------------------------------------

    def get_usage(self) -> dict[str, Any]:
        """Fetch monthly usage + allowance. Returns {ok, allowance, remaining, used, unit}."""
        key = self._current_key
        if not key:
            return {"ok": False, "error": "no key"}
        try:
            resp = httpx.get(_USAGE_URL, headers={"Authorization": f"Bearer {key}"}, timeout=15)
            data = resp.json()
        except Exception as e:
            return {"ok": False, "error": str(e)}
        info = data.get("allowanceInfo", {})
        return {
            "ok": True,
            "allowance": info.get("monthUsageAllowance"),
            "remaining": info.get("remaining"),
            "used": data.get("usage", {}).get("allowanceUsed"),
            "unit": info.get("unit", "credits"),
        }


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_client: PuterClient | None = None


def get_client() -> PuterClient:
    global _client
    if _client is None:
        _client = PuterClient()
    return _client
