"""Shared Gemini client for diary_creator: fixed model + API key rotation (config.json)."""

from __future__ import annotations

import json
import os
from typing import Any

from google import genai
from google.genai import types

# Diary pipeline always uses this model unless overridden for experiments.
DIARY_MODEL = os.environ.get("GHOSTCHAT_DIARY_MODEL", "gemini-3.1-flash-lite")


def config_candidates() -> list[str | None]:
    return [
        os.environ.get("GHOSTCHAT_API_CONFIG"),
        os.path.expanduser("~/.config/ghostchat/config.json"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "config", "config.json"),
        os.path.expanduser("~/ghostchat/config.json"),
    ]


def load_gemini_config() -> dict[str, Any]:
    for path in config_candidates():
        if not path:
            continue
        resolved = os.path.abspath(os.path.expanduser(path))
        if os.path.isfile(resolved):
            with open(resolved, encoding="utf-8") as f:
                return json.load(f)
    raise SystemExit(
        "No API config found. Set GHOSTCHAT_API_CONFIG, use ~/.config/ghostchat/config.json, "
        "GhostChat config/config.json, or ~/ghostchat/config.json with an 'api_keys' list."
    )


def _rotation_state_path() -> str:
    """Persisted key index; shared default with session bridge if GHOSTCHAT_KEY_STATE unset."""
    explicit = os.environ.get("GHOSTCHAT_KEY_STATE")
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    legacy = os.path.expanduser("~/LLM/scratch/bridge_state.json")
    if os.path.isfile(legacy):
        return legacy
    base = os.path.expanduser("~/.local/share/ghostchat")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "gemini_key_index.json")


def _read_rotation_start(n_keys: int) -> int:
    if n_keys <= 0:
        return 0
    path = _rotation_state_path()
    if not os.path.isfile(path):
        return 0
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        return int(state.get("last_index", 0)) % n_keys
    except Exception:
        return 0


def _write_rotation_next(next_index: int, n_keys: int) -> None:
    path = _rotation_state_path()
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"last_index": int(next_index) % n_keys}, f)


def generate_with_key_rotation(
    config: dict[str, Any],
    prompt: str,
    *,
    temperature: float,
    model: str | None = None,
) -> str:
    """
    Call Gemini using api_keys from config, rotating keys on each successful call
    (same scheme as GhostChat bridge_session).
    """
    keys = list(config.get("api_keys") or [])
    model_name = model or DIARY_MODEL
    if not keys:
        raise RuntimeError("Config has no 'api_keys'")

    start_index = _read_rotation_start(len(keys))
    last_error: Exception | None = None

    for i in range(len(keys)):
        current_idx = (start_index + i) % len(keys)
        api_key = keys[current_idx]
        try:
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=float(temperature),
                ),
            )
            text = (response.text or "").strip()
            if not text:
                raise RuntimeError("Empty model response")
            _write_rotation_next(current_idx + 1, len(keys))
            return text
        except Exception as e:
            last_error = e
            continue

    raise RuntimeError(f"All API keys failed for model {model_name}: {last_error}")
