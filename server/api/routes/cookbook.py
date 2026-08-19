
"""Cookbook API routes — model download, serve, presets, and lifecycle."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from engine.cookbook.state import get_state
from engine.cookbook.downloader import DownloadManager, DownloadError
from engine.cookbook.server import ServeManager, ServeError
from engine.cookbook.presets import get_presets, get_preset_by_id
from engine.cookbook.diagnose import diagnose_output
from engine.cookbook.model_settings import (
    get_all_model_settings,
    get_model_settings,
    update_model_settings,
    delete_model_settings,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["cookbook"])

_downloader = DownloadManager()
_server_mgr = ServeManager()


# ─── Status & Overview ───────────────────────────────────────────────────────

@router.get("/api/cookbook/status")
async def cookbook_status() -> dict[str, Any]:
    """Overview of cookbook state: servers, downloads, cached models."""
    state = get_state()
    state.cleanup_stale()
    return {
        "servers": [
            {
                "id": s.id,
                "model": s.model_label,
                "status": s.status,
                "port": s.port,
                "pid": s.pid,
                "ctx_size": s.ctx_size,
                "endpoint": f"http://{s.host}:{s.port}/v1",
            }
            for s in state.servers
        ],
        "downloads": [
            {
                "id": d.id,
                "repo_id": d.repo_id,
                "status": d.status,
                "progress": d.progress,
                "bytes_downloaded": d.bytes_downloaded,
                "total_bytes": d.total_bytes,
                "speed_bps": d.speed_bps,
                "include": d.include,
                "error": d.error,
                "local_dir": d.local_dir,
            }
            for d in state.downloads
        ],
        "cached_models": _downloader.scan_cached_models(),
        "settings": {
            "models_dir": str(state.models_dir),
            "default_port": state.settings.default_port,
            "default_ctx": state.settings.default_ctx,
            "default_threads": state.settings.default_threads,
            "auto_register": state.settings.auto_register,
            "has_hf_token": bool(state.settings.hf_token),
        },
    }


# ─── Downloads ────────────────────────────────────────────────────────────────

@router.post("/api/cookbook/download")
async def start_download(request: Request) -> dict[str, Any]:
    """Start a model download from HuggingFace."""
    body = await request.json()
    repo_id = body.get("repo_id", "").strip()
    if not repo_id:
        raise HTTPException(400, "Missing 'repo_id'")

    try:
        task = await _downloader.start_download(
            repo_id,
            include=body.get("include"),
            filename=body.get("filename"),
            hf_token=body.get("hf_token"),
            local_dir=body.get("local_dir"),
        )
        return {"status": "ok", "task_id": task.id, "repo_id": repo_id}
    except DownloadError as exc:
        raise HTTPException(400, str(exc))


@router.get("/api/cookbook/downloads")
async def list_downloads() -> dict[str, Any]:
    """List all download tasks."""
    return {"downloads": [
        {
            "id": d.id,
            "repo_id": d.repo_id,
            "status": d.status,
            "progress": d.progress,
            "bytes_downloaded": d.bytes_downloaded,
            "total_bytes": d.total_bytes,
            "speed_bps": d.speed_bps,
            "error": d.error,
            "local_dir": d.local_dir,
        }
        for d in _downloader.list_downloads()
    ]}


@router.delete("/api/cookbook/download/{task_id}")
async def cancel_download(task_id: str) -> dict[str, Any]:
    """Cancel an in-progress download."""
    if _downloader.cancel_download(task_id):
        return {"status": "ok", "message": "Download cancelled"}
    raise HTTPException(404, "Download not found or already finished")


# ─── Serving ──────────────────────────────────────────────────────────────────

@router.post("/api/cookbook/serve")
async def start_serve(request: Request) -> dict[str, Any]:
    """Start serving a model via llama-server."""
    body = await request.json()
    model_path = body.get("model_path", "").strip()
    if not model_path:
        raise HTTPException(400, "Missing 'model_path'")

    try:
        task = await _server_mgr.start_server(
            model_path,
            model_label=body.get("model_label", ""),
            host=body.get("host", "127.0.0.1"),
            port=body.get("port"),
            ctx_size=body.get("ctx_size"),
            threads=body.get("threads", 0),
            gpu_layers=body.get("gpu_layers", 0),
            extra_args=body.get("extra_args", ""),
        )
        return {
            "status": "ok",
            "task_id": task.id,
            "model": task.model_label,
            "endpoint": f"http://{task.host}:{task.port}/v1",
            "pid": task.pid,
        }
    except ServeError as exc:
        raise HTTPException(400, str(exc))


@router.get("/api/cookbook/servers")
async def list_servers() -> dict[str, Any]:
    """List all server instances."""
    state = get_state()
    state.cleanup_stale()
    return {"servers": [
        {
            "id": s.id,
            "model": s.model_label,
            "model_path": s.model_path,
            "status": s.status,
            "host": s.host,
            "port": s.port,
            "pid": s.pid,
            "ctx_size": s.ctx_size,
            "endpoint": f"http://{s.host}:{s.port}/v1",
            "started_at": s.started_at,
        }
        for s in state.servers
    ]}


@router.delete("/api/cookbook/server/{task_id}")
async def stop_server(task_id: str) -> dict[str, Any]:
    """Stop a running server."""
    if _server_mgr.stop_server(task_id):
        return {"status": "ok", "message": "Server stopped"}
    raise HTTPException(404, "Server not found or not running")


@router.get("/api/cookbook/server/{task_id}/logs")
async def server_logs(task_id: str, lines: int = 50) -> dict[str, Any]:
    """Tail server output logs."""
    text = _server_mgr.tail_logs(task_id, lines=lines)
    diagnosis = diagnose_output(text)
    return {
        "logs": text,
        "diagnosis": diagnosis,
    }


@router.get("/api/cookbook/server/{task_id}/health")
async def server_health(task_id: str) -> dict[str, Any]:
    """Check if a server is actually responding."""
    import httpx

    state = get_state()
    task = state.get_server(task_id)
    if not task:
        raise HTTPException(404, "Server not found")

    endpoint = f"http://{task.host}:{task.port}/v1/models"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(endpoint, headers={"Authorization": "Bearer sable-local"})
            if r.status_code == 200:
                return {"healthy": True, "endpoint": endpoint}
            return {"healthy": False, "status_code": r.status_code}
    except Exception:
        return {"healthy": False, "error": "Connection refused"}


# ─── Recommendations (hardware-aware) ─────────────────────────────────────────

@router.get("/api/cookbook/recommendations")
async def list_recommendations(ctx_size: int = 4096) -> dict[str, Any]:
    """Get models ranked by hardware compatibility."""
    import asyncio
    from engine.cookbook.presets import get_ranked_recommendations, get_hardware_summary
    from engine.cookbook.hardware import _dynamic_cache
    # Run in thread — HF API calls + scoring are CPU/IO bound, not async
    recs = await asyncio.to_thread(get_ranked_recommendations, ctx_size=ctx_size)
    return {
        "hardware": get_hardware_summary(),
        "recommendations": recs,
        "has_dynamic": _dynamic_cache is not None and len(_dynamic_cache) > 0,
    }


@router.get("/api/cookbook/hardware")
async def get_hardware_info() -> dict[str, Any]:
    """Get detected hardware specs."""
    from engine.cookbook.presets import get_hardware_summary
    return get_hardware_summary()


# ─── Presets ──────────────────────────────────────────────────────────────────

@router.get("/api/cookbook/presets")
async def list_presets() -> dict[str, Any]:
    """List top hardware-ranked presets."""
    return {"presets": get_presets()}


@router.post("/api/cookbook/serve-preset")
async def serve_preset(request: Request) -> dict[str, Any]:
    """Download (if needed) and serve a model from a preset."""
    body = await request.json()
    preset_id = body.get("preset_id", "").strip()
    if not preset_id:
        raise HTTPException(400, "Missing 'preset_id'")

    preset = get_preset_by_id(preset_id)
    if not preset:
        raise HTTPException(404, f"Preset '{preset_id}' not found")

    # Check if model already downloaded
    cached = _downloader.scan_cached_models()
    matching = [m for m in cached if preset.repo_id.split("/")[-1].lower() in m["path"].lower()]

    if matching:
        # Already have it — serve directly
        model_path = matching[0]["path"]
        try:
            task = await _server_mgr.start_server(
                model_path,
                model_label=preset.label,
                ctx_size=preset.ctx_size,
                threads=preset.threads,
                gpu_layers=preset.gpu_layers,
                extra_args=preset.extra_args,
            )
            return {
                "status": "ok",
                "served": True,
                "task_id": task.id,
                "endpoint": f"http://{task.host}:{task.port}/v1",
            }
        except ServeError as exc:
            raise HTTPException(400, str(exc))
    else:
        # Need to download first
        try:
            dl_task = await _downloader.start_download(
                preset.repo_id,
                include=preset.include,
            )
            return {
                "status": "downloading",
                "download_id": dl_task.id,
                "message": f"Downloading {preset.label}. Serve after download completes.",
            }
        except DownloadError as exc:
            raise HTTPException(400, str(exc))


# ─── Cached Models ────────────────────────────────────────────────────────────

@router.get("/api/cookbook/models")
async def list_cached_models() -> dict[str, Any]:
    """Scan for downloaded GGUF models."""
    return {"models": _downloader.scan_cached_models()}


# ─── Delete Model from Disk ────────────────────────────────────────────────────

@router.delete("/api/cookbook/model")
async def delete_model_file(request: Request) -> dict[str, Any]:
    """Delete a model GGUF file from disk."""
    body = await request.json()
    model_path = body.get("path", "")
    if not model_path:
        raise HTTPException(400, "Missing 'path'")

    from pathlib import Path as _Path
    from engine.cookbook.state import get_state

    p = _Path(model_path)
    # Safety: must be inside models_dir and must be a .gguf file
    models_dir = get_state().models_dir
    if not str(p.resolve()).startswith(str(models_dir.resolve())):
        raise HTTPException(403, "Path is outside the models directory")
    if not p.suffix == ".gguf":
        raise HTTPException(400, "Can only delete .gguf files")
    if not p.exists():
        raise HTTPException(404, "File not found")

    p.unlink()
    logger.info("Deleted model file: %s", model_path)

    # Also clean up model settings if any
    model_id = "local/" + p.stem.lower().replace(" ", "-")
    delete_model_settings(model_id)

    return {"status": "ok", "deleted": model_path}


# ─── Per-Model Instruction Settings ────────────────────────────────────────────

@router.get("/api/cookbook/model-settings")
async def list_model_settings() -> dict[str, Any]:
    """Get instruction settings for all configured models."""
    return {"settings": get_all_model_settings()}


@router.get("/api/cookbook/model-settings/{model_id:path}")
async def get_single_model_settings(model_id: str) -> dict[str, Any]:
    """Get instruction settings for a specific model."""
    return {"model_id": model_id, "settings": get_model_settings(model_id)}


@router.put("/api/cookbook/model-settings/{model_id:path}")
async def put_model_settings(model_id: str, request: Request) -> dict[str, Any]:
    """Update instruction settings for a specific model."""
    body = await request.json()
    updated = update_model_settings(model_id, body)
    return {"status": "ok", "model_id": model_id, "settings": updated}


@router.delete("/api/cookbook/model-settings/{model_id:path}")
async def del_model_settings(model_id: str) -> dict[str, Any]:
    """Remove instruction settings for a model."""
    if delete_model_settings(model_id):
        return {"status": "ok"}
    raise HTTPException(404, f"No settings found for '{model_id}'")


# ─── Settings ─────────────────────────────────────────────────────────────────

@router.get("/api/cookbook/settings")
async def get_cookbook_settings() -> dict[str, Any]:
    """Get cookbook settings."""
    state = get_state()
    return {
        "models_dir": str(state.models_dir),
        "default_port": state.settings.default_port,
        "default_ctx": state.settings.default_ctx,
        "default_threads": state.settings.default_threads,
        "default_gpu_layers": state.settings.default_gpu_layers,
        "auto_register": state.settings.auto_register,
        "has_hf_token": bool(state.settings.hf_token),
        "llama_server_bin": state.settings.llama_server_bin,
    }


@router.post("/api/cookbook/settings")
async def update_cookbook_settings(request: Request) -> dict[str, Any]:
    """Update cookbook settings."""
    body = await request.json()
    state = get_state()

    if "models_dir" in body:
        state.settings.models_dir = body["models_dir"]
    if "default_port" in body:
        state.settings.default_port = int(body["default_port"])
    if "default_ctx" in body:
        state.settings.default_ctx = int(body["default_ctx"])
    if "default_threads" in body:
        state.settings.default_threads = int(body["default_threads"])
    if "default_gpu_layers" in body:
        state.settings.default_gpu_layers = int(body["default_gpu_layers"])
    if "auto_register" in body:
        state.settings.auto_register = bool(body["auto_register"])
    if "hf_token" in body:
        state.settings.hf_token = body["hf_token"]
    if "llama_server_bin" in body:
        state.settings.llama_server_bin = body["llama_server_bin"]

    state.save()
    return {"status": "ok"}


# ─── Model Search (HuggingFace) ──────────────────────────────────────────────

@router.get("/api/cookbook/search")
async def search_models(q: str = "", limit: int = 20) -> dict[str, Any]:
    """Search HuggingFace for GGUF model repos by name."""
    if not q or len(q.strip()) < 2:
        return {"results": [], "query": q}

    query = q.strip()
    try:
        from huggingface_hub import HfApi
        state = get_state()
        api = HfApi(token=state.settings.hf_token or None)

        # Search for model repos — append "gguf" to bias toward quantized models
        models = list(api.list_models(
            search=f"{query} gguf",
            sort="downloads",
            limit=min(limit, 50),
        ))

        results = []
        for m in models:
            tags = getattr(m, "tags", []) or []
            downloads = getattr(m, "downloads", 0) or 0
            likes = getattr(m, "likes", 0) or 0

            results.append({
                "repo_id": m.id,
                "downloads": downloads,
                "likes": likes,
                "tags": [t for t in tags if t in ("gguf", "text-generation", "conversational", "base_model", "quantized")][:6],
            })

        return {"results": results[:limit], "query": query}
    except Exception as e:
        logger.error("HF search failed: %s", e)
        return {"results": [], "query": query, "error": str(e)[:200]}
