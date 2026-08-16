"""Scraper settings management: engine registry, load/save/update settings."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("sable.scraper")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
SETTINGS_PATH = BASE_DIR / "system/scraper_settings.json"
ENGINES_DIR = BASE_DIR / "engine" / "scraper_engines"

# Registry of available scraper engines
ENGINE_REGISTRY: dict[str, dict[str, str]] = {
    "qwen": {
        "label": "Qwen",
        "path": str(ENGINES_DIR / "qwen" / "qwen_engine.py"),
    },
    "deepseek": {
        "label": "DeepSeek",
        "path": str(ENGINES_DIR / "deepseek" / "deepseek_engine.py"),
    },
}

DEFAULT_ENGINE_TYPE = "qwen"

DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": False,
    "engine_type": DEFAULT_ENGINE_TYPE,
    "port": 9333,
    "headless": False,
    "show_thoughts": True,
}


def _resolve_engine_path(engine_type: str) -> str:
    """Resolve engine_type to its file path from the registry."""
    entry = ENGINE_REGISTRY.get(engine_type)
    if entry:
        return entry["path"]
    return ENGINE_REGISTRY[DEFAULT_ENGINE_TYPE]["path"]


def _load_settings() -> dict[str, Any]:
    settings = dict(DEFAULT_SETTINGS)
    if SETTINGS_PATH.exists():
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                settings.update(stored)
        except Exception as exc:
            logger.warning("Could not read system/scraper_settings.json: %s", exc)

    # Migrate legacy engine_path to engine_type
    if "engine_path" in settings and "engine_type" not in settings:
        old_path = settings.pop("engine_path", "")
        for etype, entry in ENGINE_REGISTRY.items():
            if entry["path"] == old_path:
                settings["engine_type"] = etype
                break

    # Hard requirement: scraper browser must be headed.
    settings["headless"] = False
    return settings


def get_settings() -> dict[str, Any]:
    settings = _load_settings()
    engine_type = settings.get("engine_type", DEFAULT_ENGINE_TYPE)
    engine_path = _resolve_engine_path(engine_type)
    settings["engine_path"] = engine_path
    settings["engine_exists"] = Path(engine_path).exists()
    settings["engine_label"] = ENGINE_REGISTRY.get(engine_type, {}).get("label", engine_type)
    return settings


def list_engines() -> list[dict[str, str]]:
    """Return available scraper engines for the UI."""
    return [
        {"id": etype, "label": entry["label"], "path": entry["path"]}
        for etype, entry in ENGINE_REGISTRY.items()
    ]


def save_settings(settings: dict[str, Any]) -> None:
    clean = {key: settings.get(key, DEFAULT_SETTINGS[key]) for key in DEFAULT_SETTINGS}
    clean["headless"] = False
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(clean, f, indent=2, ensure_ascii=False)


def update_settings(payload: dict[str, Any]) -> dict[str, Any]:
    settings = _load_settings()

    if "enabled" in payload:
        settings["enabled"] = bool(payload.get("enabled"))
    if "engine_type" in payload:
        engine_type = str(payload.get("engine_type") or "").strip()
        if engine_type in ENGINE_REGISTRY:
            settings["engine_type"] = engine_type
    if "port" in payload:
        try:
            settings["port"] = int(payload.get("port") or settings["port"])
        except (TypeError, ValueError):
            pass
    if "show_thoughts" in payload:
        settings["show_thoughts"] = bool(payload.get("show_thoughts"))

    # Never allow headless from the API/UI.
    settings["headless"] = False
    save_settings(settings)
    return get_settings()
