"""Persona management API — list, read, create, update, delete, set active."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import logging

from fastapi import APIRouter, HTTPException, Request

from ..dependencies import service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["personas"])

_INSTRUCTION_DIR = Path(__file__).resolve().parent.parent.parent.parent / "instruction"
_CONFIG_PATH = _INSTRUCTION_DIR / ".persona_config.json"

# Files that are NOT personas
_NON_PERSONA = {"output_format.md", "personal.md", "mem_cmd.py", "Maria.md.example"}


def _load_config() -> dict[str, Any]:
    if _CONFIG_PATH.exists():
        try:
            return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"active": "Maria", "disabled": []}


def _save_config(cfg: dict[str, Any]) -> None:
    _CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _list_personas() -> list[dict[str, Any]]:
    cfg = _load_config()
    active = cfg.get("active")
    results = []
    for f in sorted(_INSTRUCTION_DIR.glob("*.md")):
        if f.name in _NON_PERSONA:
            continue
        name = f.stem
        content = f.read_text(encoding="utf-8")
        preview = ""
        for line in content.split("\n"):
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                preview = stripped[:120]
                break
            elif stripped.startswith("#"):
                preview = stripped.lstrip("#").strip()[:120]
                break
        results.append({
            "name": name,
            "filename": f.name,
            "preview": preview,
            "active": name == active,
            "size": f.stat().st_size,
        })
    return results


# ─── Specific routes FIRST (before /{name} catch-all) ─────────────────────

@router.get("/api/personas")
async def list_personas():
    return {"personas": _list_personas(), "config": _load_config()}


@router.put("/api/personas/active")
async def set_active_persona(request: Request):
    body = await request.json()
    name = body.get("name")  # None means no active persona
    cfg = _load_config()
    if name:
        fpath = _INSTRUCTION_DIR / f"{name}.md"
        if not fpath.exists():
            raise HTTPException(404, f"Persona \'{name}\' not found")
    cfg["active"] = name
    # Auto-toggle output_format: off for Agent, on for everything else
    if name == "Agent":
        cfg["output_format_enabled"] = False
    else:
        cfg["output_format_enabled"] = True
    _save_config(cfg)
    # Bust instruction caches for all non-Qwen connectors
    from connectors.common.instruction_builder import invalidate_cache
    invalidate_cache()
    _layout_mode = body.get("layout_mode") or None
    # Only push to Qwen's personalization slot when the caller is ACTUALLY on
    # Qwen. On any other provider the persona rides in the request prompt, and
    # pushing here would spam chat.qwen.ai with settings/update calls for a
    # provider the user isn't even using. Qwen will lazily re-sync on its next
    # turn via `_ensure_qwen_layout_sync` if it's stale (see chat.py).
    _provider = (body.get("provider") or "").lower()
    _on_qwen = _provider in ("qwen", "qwen_scraper")
    if _on_qwen:
        try:
            ok = await service.sync_context(layout_mode=_layout_mode)
            if ok:
                try:
                    from .chat import mark_qwen_synced as _mark
                    _mark(_layout_mode)
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("sync_context after persona switch failed: %s", exc)
    return {"status": "ok", "active": name, "qwen_synced": _on_qwen}


@router.get("/api/personas/output-format")
async def get_output_format():
    fpath = _INSTRUCTION_DIR / "output_format.md"
    if not fpath.exists():
        return {"content": ""}
    return {"content": fpath.read_text(encoding="utf-8")}


@router.put("/api/personas/output-format")
async def update_output_format(request: Request):
    body = await request.json()
    fpath = _INSTRUCTION_DIR / "output_format.md"
    fpath.write_text(body.get("content", ""), encoding="utf-8")
    from connectors.common.instruction_builder import invalidate_cache
    invalidate_cache()
    return {"status": "ok"}


@router.put("/api/personas/output-format-toggle")
async def toggle_output_format(request: Request):
    body = await request.json()
    enabled = body.get("enabled", True)
    cfg = _load_config()
    cfg["output_format_enabled"] = bool(enabled)
    _save_config(cfg)
    from connectors.common.instruction_builder import invalidate_cache
    invalidate_cache()
    _layout_mode = body.get("layout_mode") or None
    # Same conditional as set_active_persona: only push to Qwen when the
    # caller is actually on Qwen. Other providers ride the prompt and Qwen
    # re-syncs lazily on its next turn.
    _provider = (body.get("provider") or "").lower()
    _on_qwen = _provider in ("qwen", "qwen_scraper")
    if _on_qwen:
        try:
            ok = await service.sync_context(layout_mode=_layout_mode)
            if ok:
                try:
                    from .chat import mark_qwen_synced as _mark
                    _mark(_layout_mode)
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("sync_context after output_format toggle failed: %s", exc)
    return {"status": "ok", "output_format_enabled": enabled, "qwen_synced": _on_qwen}


# ─── Dynamic /{name} routes LAST ──────────────────────────────────────────

@router.get("/api/personas/{name}")
async def get_persona(name: str):
    fpath = _INSTRUCTION_DIR / f"{name}.md"
    if not fpath.exists() or fpath.name in _NON_PERSONA:
        raise HTTPException(404, f"Persona \'{name}\' not found")
    return {"name": name, "content": fpath.read_text(encoding="utf-8")}


@router.post("/api/personas")
async def create_persona(request: Request):
    body = await request.json()
    name = body.get("name", "").strip()
    content = body.get("content", "")
    if not name:
        raise HTTPException(400, "Name required")
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    fpath = _INSTRUCTION_DIR / f"{safe_name}.md"
    if fpath.exists():
        raise HTTPException(409, f"Persona \'{safe_name}\' already exists")
    fpath.write_text(content, encoding="utf-8")
    from connectors.common.instruction_builder import invalidate_cache
    invalidate_cache()
    return {"status": "ok", "name": safe_name}


@router.put("/api/personas/{name}")
async def update_persona(name: str, request: Request):
    fpath = _INSTRUCTION_DIR / f"{name}.md"
    if not fpath.exists():
        raise HTTPException(404, f"Persona \'{name}\' not found")
    body = await request.json()
    content = body.get("content", "")
    fpath.write_text(content, encoding="utf-8")
    from connectors.common.instruction_builder import invalidate_cache
    invalidate_cache()
    return {"status": "ok"}


@router.delete("/api/personas/{name}")
async def delete_persona(name: str):
    fpath = _INSTRUCTION_DIR / f"{name}.md"
    if not fpath.exists():
        raise HTTPException(404, f"Persona \'{name}\' not found")
    fpath.unlink()
    cfg = _load_config()
    if cfg.get("active") == name:
        cfg["active"] = None
    _save_config(cfg)
    from connectors.common.instruction_builder import invalidate_cache
    invalidate_cache()
    return {"status": "ok"}
