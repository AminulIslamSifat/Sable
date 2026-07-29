from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from engine.config import MODELS, BROWSER_DATA_DIR, get_model_config
from engine.scraper import (
    get_settings as get_scraper_settings,
    list_engines as list_scraper_engines,
    scraper as scraper_service,
    update_settings as update_scraper_settings,
)
from engine.memory_search import get_searcher, list_available_models
from engine.service import ChatService
from engine.skills import BACKUP_DIR, SkillParser, browse_skills, build_tool_feedback, list_skills
from connectors.deepseek.client import get_client as get_deepseek_client
from instruction.mem_cmd import _CONSOLIDATE_PROMPT_TEMPLATE_HISTORY, _CONSOLIDATE_PROMPT_TEMPLATE_STANDALONE

# Activate logging and import submodules
from . import logging_setup  # noqa

from .config import (
    BASE_DIR, WEB_DIR, INDEX_FILE, UPLOAD_DIR,
    AUTH_EXEMPT_PREFIXES,
    SKILL_ROUND_WARN_THRESHOLD,
    DEEPSEEK_MODELS,
    _SYSTEM_DIR, _ACTIVE_PROFILE_LINK, _BROWSER_PROFILES,
    _MEMORY_PATH, _PROTECTED_PATH, _MEMORY_SEARCH_SETTINGS, _DEFAULT_MAX_PROMPT_CHARS,
)
from .database import (
    init_db, ensure_chat, get_chat_mode, get_chat_provider,
    set_title_if_default, get_injected_memory_keys, save_injected_memory_keys,
    touch_chat, save_chat_url, get_chat_url,
    add_message, update_message, get_messages, list_chats, delete_chat, get_parent_id,
)
from .auth import AUTH_TOKEN
from .utils import (
    utcnow, retry_async, retry_stream, make_title, _build_conversation_summary,
    _is_deepseek_api_model, _dir_size_mb, _read_profile_email, logger,
)
from .models import LoginRequest, RevertRequest, ChatRequest, NewChatRequest
from .logging_setup import _log_buffer

# Global service
service = ChatService(user_data_dir=str(BROWSER_DATA_DIR))
get_deepseek_client().set_token_refresher(service.refresh_deepseek_token)

def sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

@asynccontextmanager
async def lifespan(app: FastAPI) -> Generator[None, None, None]:
    init_db()
    if _MEMORY_SEARCH_SETTINGS.exists():
        try:
            _ms = json.loads(_MEMORY_SEARCH_SETTINGS.read_text(encoding="utf-8"))
            _s = get_searcher()
            if _ms.get("model"):
                _s.set_model(str(_ms["model"]))
            if isinstance(_ms.get("model_thresholds"), dict):
                _s.set_thresholds(_ms["model_thresholds"])
        except Exception:
            pass
    await service.warmup()
    try:
        ds_token = await service.refresh_deepseek_token()
        get_deepseek_client().set_token(ds_token)
    except Exception as exc:
        logger.warning("DeepSeek startup token refresh failed: %s: %s", type(exc).__name__, exc)
    yield
    await service.close()
    await scraper_service.stop(kill_browser=True)

app = FastAPI(title="Sable API", version="0.4.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

if UPLOAD_DIR.exists():
    app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# ---------- Auth ----------
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/") or any(path.startswith(p) for p in AUTH_EXEMPT_PREFIXES):
        return await call_next(request)
    auth_header = request.headers.get("authorization", "")
    authorized = auth_header.startswith("Bearer ") and auth_header[7:] == AUTH_TOKEN
    if not authorized and path == "/api/logs":
        authorized = request.query_params.get("token", "") == AUTH_TOKEN
    if not authorized:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    return await call_next(request)

@app.post("/api/login")
async def login(payload: LoginRequest) -> dict[str, str]:
    if payload.token.strip() != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")
    return {"status": "ok"}

@app.post("/api/file/revert")
def revert_file(payload: RevertRequest) -> dict[str, str]:
    backup = Path(payload.backup_path).expanduser()
    target = Path(payload.path).expanduser()
    try:
        backup.resolve().relative_to(BACKUP_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Backup outside managed directory")
    if not backup.is_file():
        raise HTTPException(status_code=404, detail="Backup not found")
    try:
        shutil.copy2(backup, target)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Revert failed: {exc}")
    return {"status": "ok"}

@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

@app.get("/api/logs")
async def stream_logs():
    async def generator():
        while True:
            try:
                msg = await asyncio.wait_for(_log_buffer.get(), timeout=15.0)
                yield sse({"type": "log", "message": msg})
            except asyncio.TimeoutError:
                yield sse({"type": "ping"})
    return StreamingResponse(generator(), media_type="text/event-stream")

@app.get("/api/chats")
def chats() -> dict[str, list[dict[str, Any]]]:
    return {"chats": list_chats()}

@app.get("/api/models")
def models() -> dict[str, list[dict[str, Any]]]:
    scraper_cfg = get_scraper_settings()
    if scraper_cfg.get("enabled") and scraper_cfg.get("engine_type") == "deepseek":
        return {"models": DEEPSEEK_MODELS}
    return {
        "models": [
            {
                "id": m["id"],
                "label": m["label"],
                "api_backend": m.get("api_backend"),
                "thinking_modes": [
                    {"id": tm["id"], "label": tm["label"]} for tm in m["thinking_modes"]
                ],
            }
            for m in MODELS
        ]
    }

@app.post("/api/chat/new")
async def new_chat(request: NewChatRequest = NewChatRequest()) -> dict[str, str | None]:
    if get_scraper_settings().get("enabled"):
        chat_id = f"browser-{uuid.uuid4().hex}"
        ensure_chat(chat_id, "New chat", None)
        return {"chat_id": chat_id}
    try:
        chat_id = await retry_async(
            lambda: service.create_chat(model=request.model),
            label="create_chat",
        )
    except Exception as exc:
        return {"error": f"Session startup failed: {type(exc).__name__}: {exc}"}
    if not chat_id:
        return {"error": "Could not create chat session"}
    ensure_chat(chat_id, "New chat", None)
    return {"chat_id": chat_id}

@app.get("/api/chats/{chat_id}/messages")
def chat_messages(chat_id: str) -> dict[str, Any]:
    return {"chat_id": chat_id, "messages": get_messages(chat_id)}

@app.delete("/api/chats/{chat_id}")
def delete_chat_route(chat_id: str) -> dict[str, Any]:
    deleted = delete_chat(chat_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Chat not found")
    return {"deleted": True, "chat_id": chat_id}

@app.get("/api/skills")
def skills() -> dict[str, list[dict[str, Any]]]:
    return {"skills": list_skills()}

@app.get("/api/skills/browse")
def skills_browse() -> dict[str, list[dict[str, Any]]]:
    return {"skills": browse_skills()}

@app.post("/api/sync-context")
async def sync_context_route() -> dict[str, Any]:
    success = await service.sync_context()
    if success:
        return {"status": "ok", "message": "Context synced successfully"}
    raise HTTPException(status_code=500, detail="Failed to sync context")

@app.get("/api/settings/scraper")
async def get_scraper_settings_route() -> dict[str, Any]:
    return get_scraper_settings()

@app.get("/api/settings/scraper/engines")
async def get_scraper_engines_route() -> dict[str, Any]:
    return {"engines": list_scraper_engines()}

@app.post("/api/settings/scraper")
async def update_scraper_settings_route(payload: dict[str, Any]) -> dict[str, Any]:
    old_settings = get_scraper_settings()
    settings = update_scraper_settings(payload)
    engine_changed = old_settings.get("engine_type") != settings.get("engine_type")
    toggled_off = old_settings.get("enabled") and not settings.get("enabled")
    if engine_changed or toggled_off:
        await scraper_service.stop()
    if settings.get("enabled"):
        prelaunch_result = await scraper_service.prelaunch()
        settings["prelaunch"] = prelaunch_result
    return settings

@app.get("/api/scraper/sessions")
async def get_scraper_sessions() -> dict[str, Any]:
    return await scraper_service.get_session_info()

@app.post("/api/scraper/sessions/kill")
async def kill_scraper_session() -> dict[str, Any]:
    return await scraper_service.kill_session()

@app.post("/api/scraper/model")
async def switch_scraper_model(payload: dict[str, Any]) -> dict[str, Any]:
    model_type = str(payload.get("model_type") or "default").strip()
    return await scraper_service.switch_model(model_type)

@app.get("/api/settings/browser")
async def get_browser_settings() -> dict[str, bool]:
    return {"headless": service.browser_headless}

@app.post("/api/settings/browser")
async def update_browser_settings(payload: dict[str, bool]) -> dict[str, Any]:
    headless = payload.get("headless")
    if headless is None:
        raise HTTPException(status_code=400, detail="Missing 'headless' field")
    await service.restart_browser(headless=headless)
    return {"status": "ok", "headless": service.browser_headless}

@app.post("/api/settings/deepseek/refresh-token")
async def refresh_deepseek_token() -> dict[str, Any]:
    try:
        token = await get_deepseek_client().refresh_token()
        return {"status": "ok", "token_preview": token[:20] + "...", "active": True}
    except Exception as exc:
        logger.error("DeepSeek token refresh failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Refresh failed: {exc}")

@app.get("/api/settings/accounts")
async def list_accounts() -> dict[str, Any]:
    def _scan() -> list[dict[str, Any]]:
        accounts: list[dict[str, Any]] = []
        for entry in sorted(_SYSTEM_DIR.iterdir()):
            if entry.is_dir() and entry.name.startswith("browser-data-acc"):
                accounts.append({
                    "name": entry.name,
                    "email": _read_profile_email(entry),
                    "size_mb": _dir_size_mb(entry),
                })
        return accounts
    accounts = await asyncio.to_thread(_scan)
    active: str | None = None
    if _ACTIVE_PROFILE_LINK.is_symlink():
        target = _ACTIVE_PROFILE_LINK.resolve().name
        active = target
    elif _ACTIVE_PROFILE_LINK.is_dir():
        active = "browser-data (not yet migrated)"
    return {"accounts": accounts, "active": active}

@app.post("/api/settings/accounts/switch")
async def switch_account(payload: dict[str, str]) -> dict[str, Any]:
    target_name = payload.get("profile", "")
    if not target_name.startswith("browser-data-acc"):
        raise HTTPException(status_code=400, detail="Profile must match 'browser-data-acc*'")
    target_path = _SYSTEM_DIR / target_name
    if not target_path.is_dir():
        raise HTTPException(status_code=404, detail=f"Profile directory '{target_name}' not found")
    def _do_switch() -> None:
        if _ACTIVE_PROFILE_LINK.is_dir() and not _ACTIVE_PROFILE_LINK.is_symlink():
            migration_name = "browser-data-acc1"
            migration_path = _SYSTEM_DIR / migration_name
            if migration_path.exists():
                shutil.rmtree(_ACTIVE_PROFILE_LINK)
            else:
                _ACTIVE_PROFILE_LINK.rename(migration_path)
        elif _ACTIVE_PROFILE_LINK.is_symlink():
            _ACTIVE_PROFILE_LINK.unlink()
        _ACTIVE_PROFILE_LINK.symlink_to(target_path)
    await service.close()
    try:
        await asyncio.to_thread(_do_switch)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Switch failed: {exc}")
    await service.warmup()
    try:
        ds_token = await service.refresh_deepseek_token()
        get_deepseek_client().set_token(ds_token)
    except Exception:
        pass
    return {"status": "ok", "active": target_name, "email": _read_profile_email(target_path)}

@app.get("/api/settings/browser/profiles")
async def get_browser_profiles() -> dict[str, Any]:
    def _collect() -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, (data_path, bak_path) in _BROWSER_PROFILES.items():
            result[key] = {
                "label": {"api": "API (ChatService)", "scraper": "Scraper", "automation": "Automation (Browser Control)"}[key],
                "data_dir": str(data_path.relative_to(BASE_DIR)),
                "exists": data_path.is_dir(),
                "size_mb": _dir_size_mb(data_path),
                "has_backup": bak_path.is_dir(),
                "backup_size_mb": _dir_size_mb(bak_path),
            }
        return result
    result = await asyncio.to_thread(_collect)
    return {"profiles": result}

@app.post("/api/settings/browser/restore")
async def restore_browser_profile(payload: dict[str, str]) -> dict[str, Any]:
    profile = payload.get("profile", "")
    if profile not in _BROWSER_PROFILES:
        raise HTTPException(status_code=400, detail=f"Unknown profile '{profile}'. Use 'api', 'scraper', or 'automation'.")
    data_path, bak_path = _BROWSER_PROFILES[profile]
    if not bak_path.is_dir():
        raise HTTPException(status_code=404, detail=f"No backup found at {bak_path}")
    def _do_restore() -> None:
        if data_path.is_dir():
            shutil.rmtree(data_path)
        shutil.copytree(bak_path, data_path, symlinks=True)
    await asyncio.to_thread(_do_restore)
    return {
        "status": "ok",
        "profile": profile,
        "restored_from": str(bak_path),
        "restored_to": str(data_path),
    }

@app.post("/api/settings/browser/create-backup")
async def create_browser_backup(payload: dict[str, str]) -> dict[str, Any]:
    profile = payload.get("profile", "")
    if profile not in _BROWSER_PROFILES:
        raise HTTPException(status_code=400, detail=f"Unknown profile '{profile}'. Use 'api', 'scraper', or 'automation'.")
    data_path, bak_path = _BROWSER_PROFILES[profile]
    if not data_path.is_dir():
        raise HTTPException(status_code=404, detail=f"No data found at {data_path}")
    def _do_backup() -> None:
        if bak_path.is_dir():
            shutil.rmtree(bak_path)
        shutil.copytree(data_path, bak_path, symlinks=True)
    await asyncio.to_thread(_do_backup)
    return {
        "status": "ok",
        "profile": profile,
        "backed_up": str(data_path),
        "backup_to": str(bak_path),
        "size_mb": _dir_size_mb(bak_path),
    }

@app.get("/api/settings/memory")
async def get_memory() -> dict[str, Any]:
    if not _MEMORY_PATH.exists():
        return {"memory": {"semantic": [], "episodic": [], "procedural": [], "ephemeral": []}}
    try:
        data = json.loads(_MEMORY_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for cat in ("semantic", "episodic", "procedural", "ephemeral"):
                data.setdefault(cat, [])
            return {"memory": data}
        return {"memory": {"semantic": [], "episodic": [], "procedural": [], "ephemeral": []}}
    except Exception:
        return {"memory": {"semantic": [], "episodic": [], "procedural": [], "ephemeral": []}}

@app.post("/api/settings/memory")
async def update_memory(payload: dict[str, Any]) -> dict[str, str]:
    memory = payload.get("memory")
    if memory is None:
        raise HTTPException(status_code=400, detail="Missing 'memory' field")
    _MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _MEMORY_PATH.write_text(json.dumps(memory, indent=2, ensure_ascii=False), encoding="utf-8")
    get_searcher().reload_memory()
    return {"status": "ok"}

@app.get("/api/settings/memory/protected")
async def get_protected_memory() -> dict[str, Any]:
    if not _PROTECTED_PATH.exists():
        return {"protected": []}
    try:
        data = json.loads(_PROTECTED_PATH.read_text(encoding="utf-8"))
        entries = data.get("protected", []) if isinstance(data, dict) else []
        return {"protected": entries}
    except Exception:
        return {"protected": []}

@app.post("/api/settings/memory/protected")
async def update_protected_memory(payload: dict[str, Any]) -> dict[str, str]:
    protected = payload.get("protected")
    if protected is None:
        raise HTTPException(status_code=400, detail="Missing 'protected' field")
    _PROTECTED_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PROTECTED_PATH.write_text(
        json.dumps({"protected": protected}, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    get_searcher().reload_memory()
    return {"status": "ok"}

@app.get("/api/settings/memory-search")
async def get_memory_search_settings() -> dict[str, Any]:
    searcher = get_searcher()
    cfg: dict[str, Any] = {"enabled": True, "top_k": 10}
    if _MEMORY_SEARCH_SETTINGS.exists():
        try:
            cfg = json.loads(_MEMORY_SEARCH_SETTINGS.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "enabled": cfg.get("enabled", True),
        "top_k": cfg.get("top_k", 10),
        "max_prompt_chars": cfg.get("max_prompt_chars", _DEFAULT_MAX_PROMPT_CHARS),
        "model_thresholds": searcher.get_custom_thresholds(),
        "current_model": searcher.model_name,
        "current_threshold": searcher.threshold,
        "available_models": list_available_models(),
    }

@app.post("/api/settings/memory-search")
async def update_memory_search_settings(payload: dict[str, Any]) -> dict[str, Any]:
    model = payload.get("model")
    top_k = payload.get("top_k")
    enabled = payload.get("enabled")
    max_prompt_chars = payload.get("max_prompt_chars")
    model_thresholds = payload.get("model_thresholds")
    cfg: dict[str, Any] = {}
    if _MEMORY_SEARCH_SETTINGS.exists():
        try:
            cfg = json.loads(_MEMORY_SEARCH_SETTINGS.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    if model is not None:
        cfg["model"] = str(model)
        get_searcher().set_model(str(model))
    if isinstance(model_thresholds, dict):
        clean: dict[str, float] = {}
        for k, v in model_thresholds.items():
            try:
                if v not in (None, "", "auto"):
                    clean[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
        cfg["model_thresholds"] = clean
        get_searcher().set_thresholds(clean)
    if top_k is not None:
        cfg["top_k"] = int(top_k)
    if max_prompt_chars is not None:
        cfg["max_prompt_chars"] = max(1000, int(max_prompt_chars))
    if enabled is not None:
        cfg["enabled"] = bool(enabled)
    _MEMORY_SEARCH_SETTINGS.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    searcher = get_searcher()
    return {
        "status": "ok",
        "current_model": searcher.model_name,
        "current_threshold": searcher.threshold,
    }

@app.post("/api/memory/consolidate")
async def consolidate_memory(payload: dict[str, Any]) -> dict[str, Any]:
    chat_id = payload.get("chat_id")
    model = payload.get("model")
    mode = payload.get("mode", "scraper")
    if not chat_id:
        raise HTTPException(status_code=400, detail="Missing 'chat_id'")
    messages = get_messages(chat_id)
    if len(messages) < 2:
        return {"status": "skipped", "reason": "too few messages"}
    injected_keys = get_injected_memory_keys(chat_id)
    filtered_memory: dict[str, list] = {}
    if _MEMORY_PATH.exists() and injected_keys:
        try:
            full_memory = json.loads(_MEMORY_PATH.read_text(encoding="utf-8"))
            if isinstance(full_memory, dict):
                for category, entries in full_memory.items():
                    if isinstance(entries, list):
                        matched = [e for e in entries if isinstance(e, dict) and e.get("key") in injected_keys]
                        if matched:
                            filtered_memory[category] = matched
        except Exception:
            pass
    current_memory = json.dumps(filtered_memory, indent=2) if filtered_memory else "{}"
    if mode == "api":
        prompt = _CONSOLIDATE_PROMPT_TEMPLATE_STANDALONE
        prompt = prompt.replace("<<CURRENT_MEMORY>>", current_memory)
        prompt = prompt.replace("<<CONVERSATION_SUMMARY>>", "(See conversation thread above — do not request more context.)")
        parent_id = get_parent_id(chat_id, None)
        try:
            if _is_deepseek_api_model(model):
                _ds_cfg = get_model_config(model)
                _ds_api_type = _ds_cfg.get("api_model_type")
                result = await retry_async(
                    lambda: get_deepseek_client().chat(
                        message=prompt,
                        model=_ds_api_type,
                        thinking_mode="fast",
                        chat_id=chat_id,
                        inject_instructions=False,
                    ),
                    label="memory_consolidate_api_ds",
                )
            else:
                result = await retry_async(
                    lambda: service.chat(
                        message=prompt,
                        chat_id=chat_id,
                        parent_id=parent_id,
                        model=model,
                    ),
                    label="memory_consolidate_api",
                )
        except Exception as exc:
            return {"status": "error", "detail": f"Model call failed: {exc}"}
    else:
        prompt = _CONSOLIDATE_PROMPT_TEMPLATE_HISTORY.replace("<<CURRENT_MEMORY>>", current_memory)
        try:
            result = await retry_async(
                lambda: service.chat(
                    message=prompt,
                    chat_id=chat_id,
                    model=model,
                ),
                label="memory_consolidate_scraper",
            )
        except Exception as exc:
            return {"status": "error", "detail": f"Model call failed: {exc}"}
    raw_answer = str(result.get("answer", ""))
    cleaned = raw_answer.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()
    try:
        new_entries = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end > start:
            try:
                new_entries = json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                return {"status": "error", "detail": "Model returned invalid JSON", "raw": raw_answer[:500]}
        else:
            return {"status": "error", "detail": "No JSON object found in response", "raw": raw_answer[:500]}
    if not isinstance(new_entries, dict):
        return {"status": "error", "detail": "Expected dict with add/delete keys"}
    if "add" in new_entries:
        adds = new_entries["add"]
        deletes = new_entries.get("delete", [])
    else:
        adds = {k: v for k, v in new_entries.items() if k in ("semantic", "episodic", "procedural", "protected", "ephemeral")}
        deletes = []
    if not isinstance(adds, dict):
        adds = {}
    if not isinstance(deletes, list):
        deletes = []
    existing: dict[str, list[dict[str, str]]] = {}
    if _MEMORY_PATH.exists():
        try:
            existing = json.loads(_MEMORY_PATH.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except Exception:
            existing = {}
    protected_keys: set[str] = set()
    for e in existing.get("protected", []):
        if isinstance(e, dict) and e.get("key"):
            protected_keys.add(e["key"])
    for entry in adds.get("protected", []):
        if isinstance(entry, dict) and entry.get("key"):
            protected_keys.add(entry["key"])
    deleted_count = 0
    delete_keys = {str(k) for k in deletes if k} - protected_keys
    if delete_keys:
        for cat in ("semantic", "episodic", "procedural", "ephemeral"):
            cat_list = existing.get(cat, [])
            before = len(cat_list)
            existing[cat] = [e for e in cat_list if not isinstance(e, dict) or e.get("key", "") not in delete_keys]
            deleted_count += before - len(existing[cat])
    added_count = 0
    for cat in ("semantic", "episodic", "procedural"):
        existing_list = existing.get(cat, [])
        new_list = adds.get(cat, [])
        if not isinstance(new_list, list):
            continue
        existing_keys = {e.get("key", "") for e in existing_list if isinstance(e, dict)}
        for entry in new_list:
            if isinstance(entry, dict) and entry.get("key") and entry["key"] not in existing_keys:
                existing_list.append({"key": entry["key"], "value": entry.get("value", "")})
                existing_keys.add(entry["key"])
                added_count += 1
        existing[cat] = existing_list
    prot_new = adds.get("protected", [])
    prot_added = 0
    if isinstance(prot_new, list) and prot_new:
        existing_prot: list[dict[str, str]] = []
        if _PROTECTED_PATH.exists():
            try:
                pdata = json.loads(_PROTECTED_PATH.read_text(encoding="utf-8"))
                existing_prot = pdata.get("protected", []) if isinstance(pdata, dict) else []
            except Exception:
                existing_prot = []
        prot_keys = {e.get("key", "") for e in existing_prot if isinstance(e, dict)}
        for entry in prot_new:
            if isinstance(entry, dict) and entry.get("key") and entry["key"] not in prot_keys:
                existing_prot.append({"key": entry["key"], "value": entry.get("value", "")})
                prot_keys.add(entry["key"])
                prot_added += 1
        _PROTECTED_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PROTECTED_PATH.write_text(
            json.dumps({"protected": existing_prot}, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    eph_new = adds.get("ephemeral", [])
    eph_added = 0
    if isinstance(eph_new, list) and eph_new:
        eph_list = existing.get("ephemeral", [])
        eph_keys = {e.get("key", "") for e in eph_list if isinstance(e, dict)}
        for entry in eph_new:
            if isinstance(entry, dict) and entry.get("key") and entry["key"] not in eph_keys:
                eph_entry: dict[str, Any] = {"key": entry["key"], "value": entry.get("value", "")}
                if entry.get("expires_at"):
                    eph_entry["expires_at"] = str(entry["expires_at"])
                eph_list.append(eph_entry)
                eph_keys.add(entry["key"])
                eph_added += 1
        existing["ephemeral"] = eph_list
    _MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _MEMORY_PATH.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    get_searcher().reload_memory()
    total_added = added_count + prot_added + eph_added
    return {"status": "ok", "added": total_added, "deleted": deleted_count}

@app.post("/api/memory/consolidate-scraper")
async def consolidate_memory_scraper(payload: dict[str, Any]) -> dict[str, Any]:
    chat_id = payload.get("chat_id")
    model = payload.get("model")
    if not chat_id:
        raise HTTPException(status_code=400, detail="Missing 'chat_id'")
    messages = get_messages(chat_id)
    if len(messages) < 2:
        return {"status": "skipped", "reason": "too few messages"}
    injected_keys = get_injected_memory_keys(chat_id)
    filtered_memory: dict[str, list] = {}
    if _MEMORY_PATH.exists() and injected_keys:
        try:
            full_memory = json.loads(_MEMORY_PATH.read_text(encoding="utf-8"))
            if isinstance(full_memory, dict):
                for category, entries in full_memory.items():
                    if isinstance(entries, list):
                        matched = [e for e in entries if isinstance(e, dict) and e.get("key") in injected_keys]
                        if matched:
                            filtered_memory[category] = matched
        except Exception:
            pass
    current_memory = json.dumps(filtered_memory, indent=2) if filtered_memory else "{}"
    prompt = _CONSOLIDATE_PROMPT_TEMPLATE_HISTORY.replace("<<CURRENT_MEMORY>>", current_memory)
    answer_parts: list[str] = []
    error_msg: str | None = None
    try:
        async for event in scraper_service.stream_events(
            message=prompt,
            model=model,
            raw=True,
        ):
            etype = event.get("type")
            if etype == "answer":
                answer_parts.append(str(event.get("text", "")))
            elif etype == "error":
                error_msg = str(event.get("message", "Unknown error"))
    except Exception as exc:
        return {"status": "error", "detail": f"Scraper stream failed: {exc}"}
    if error_msg:
        return {"status": "error", "detail": error_msg}
    raw_answer = "".join(answer_parts)
    cleaned = raw_answer.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()
    try:
        new_entries = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end > start:
            try:
                new_entries = json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                return {"status": "error", "detail": "Model returned invalid JSON", "raw": raw_answer[:500]}
        else:
            return {"status": "error", "detail": "No JSON object found in response", "raw": raw_answer[:500]}
    if not isinstance(new_entries, dict):
        return {"status": "error", "detail": "Expected dict with add/delete keys"}
    if "add" in new_entries:
        adds = new_entries["add"]
        deletes = new_entries.get("delete", [])
    else:
        adds = {k: v for k, v in new_entries.items() if k in ("semantic", "episodic", "procedural", "protected", "ephemeral")}
        deletes = []
    if not isinstance(adds, dict):
        adds = {}
    if not isinstance(deletes, list):
        deletes = []
    existing: dict[str, list[dict[str, str]]] = {}
    if _MEMORY_PATH.exists():
        try:
            existing = json.loads(_MEMORY_PATH.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except Exception:
            existing = {}
    protected_keys: set[str] = set()
    for e in existing.get("protected", []):
        if isinstance(e, dict) and e.get("key"):
            protected_keys.add(e["key"])
    for entry in adds.get("protected", []):
        if isinstance(entry, dict) and entry.get("key"):
            protected_keys.add(entry["key"])
    deleted_count = 0
    delete_keys = {str(k) for k in deletes if k} - protected_keys
    if delete_keys:
        for cat in ("semantic", "episodic", "procedural", "ephemeral"):
            cat_list = existing.get(cat, [])
            before = len(cat_list)
            existing[cat] = [e for e in cat_list if not isinstance(e, dict) or e.get("key", "") not in delete_keys]
            deleted_count += before - len(existing[cat])
    added_count = 0
    for cat in ("semantic", "episodic", "procedural"):
        existing_list = existing.get(cat, [])
        new_list = adds.get(cat, [])
        if not isinstance(new_list, list):
            continue
        existing_keys = {e.get("key", "") for e in existing_list if isinstance(e, dict)}
        for entry in new_list:
            if isinstance(entry, dict) and entry.get("key") and entry["key"] not in existing_keys:
                existing_list.append({"key": entry["key"], "value": entry.get("value", "")})
                existing_keys.add(entry["key"])
                added_count += 1
        existing[cat] = existing_list
    prot_new = adds.get("protected", [])
    prot_added = 0
    if isinstance(prot_new, list) and prot_new:
        existing_prot: list[dict[str, str]] = []
        if _PROTECTED_PATH.exists():
            try:
                pdata = json.loads(_PROTECTED_PATH.read_text(encoding="utf-8"))
                existing_prot = pdata.get("protected", []) if isinstance(pdata, dict) else []
            except Exception:
                existing_prot = []
        prot_keys = {e.get("key", "") for e in existing_prot if isinstance(e, dict)}
        for entry in prot_new:
            if isinstance(entry, dict) and entry.get("key") and entry["key"] not in prot_keys:
                existing_prot.append({"key": entry["key"], "value": entry.get("value", "")})
                prot_keys.add(entry["key"])
                prot_added += 1
        _PROTECTED_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PROTECTED_PATH.write_text(
            json.dumps({"protected": existing_prot}, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    eph_new = adds.get("ephemeral", [])
    eph_added = 0
    if isinstance(eph_new, list) and eph_new:
        eph_list = existing.get("ephemeral", [])
        eph_keys = {e.get("key", "") for e in eph_list if isinstance(e, dict)}
        for entry in eph_new:
            if isinstance(entry, dict) and entry.get("key") and entry["key"] not in eph_keys:
                eph_entry: dict[str, Any] = {"key": entry["key"], "value": entry.get("value", "")}
                if entry.get("expires_at"):
                    eph_entry["expires_at"] = str(entry["expires_at"])
                eph_list.append(eph_entry)
                eph_keys.add(entry["key"])
                eph_added += 1
        existing["ephemeral"] = eph_list
    _MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _MEMORY_PATH.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    get_searcher().reload_memory()
    total_added = added_count + prot_added + eph_added
    return {"status": "ok", "added": total_added, "deleted": deleted_count}

@app.post("/api/upload")
async def upload(file: UploadFile = File(...)) -> dict[str, Any]:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")
    suffix = Path(file.filename).suffix or ".bin"
    stored_name = f"{uuid.uuid4().hex}{suffix}"
    target = UPLOAD_DIR / stored_name
    raw = await file.read()
    target.write_bytes(raw)
    result = await service.upload_image(str(target))
    if result is None:
        return {"uploaded": False, "path": str(target)}
    return {"uploaded": True, "path": str(target), "meta": result}

@app.post("/api/chat")
async def chat(request: ChatRequest):
    scraper_enabled = get_scraper_settings().get("enabled")
    active_chat_id = request.chat_id
    if not active_chat_id and scraper_enabled:
        active_chat_id = f"browser-{uuid.uuid4().hex}"
    if not active_chat_id:
        try:
            active_chat_id = await retry_async(
                lambda: service.create_chat(model=request.model),
                label="create_chat",
            )
        except Exception as exc:
            return {"error": f"Session startup failed: {type(exc).__name__}: {exc}"}
        if not active_chat_id:
            return {"error": "Could not create chat session"}
    timestamped_message = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]\n{request.message}"
    _injected_memory_keys = get_injected_memory_keys(active_chat_id)
    _ms_cfg: dict[str, Any] = {"enabled": True, "top_k": 10}
    _searcher = get_searcher()
    _memory_used: list[dict[str, Any]] = []
    try:
        if _MEMORY_SEARCH_SETTINGS.exists():
            _ms_cfg = json.loads(_MEMORY_SEARCH_SETTINGS.read_text(encoding="utf-8"))
        _max_chars = _ms_cfg.get("max_prompt_chars", _DEFAULT_MAX_PROMPT_CHARS)
        if _ms_cfg.get("enabled", True) and len(request.message) <= _max_chars:
            _mem_results = _searcher.search(request.message, top_k=_ms_cfg.get("top_k", 10))
            _new_results = [r for r in _mem_results if r.get("key") and r["key"] not in _injected_memory_keys]
            if _new_results:
                _mem_block = _searcher.format_for_prompt(_new_results)
                if _mem_block:
                    timestamped_message = f"{_mem_block}\n\n{timestamped_message}"
                    for r in _new_results:
                        _injected_memory_keys.add(r["key"])
                    save_injected_memory_keys(active_chat_id, _injected_memory_keys)
                    _memory_used = [
                        {
                            "key": r.get("key", ""),
                            "value": r.get("value", ""),
                            "category": r.get("category", ""),
                            "score": round(float(r.get("score", 0.0)), 3),
                        }
                        for r in _new_results
                    ]
    except Exception:
        pass
    current_mode = "scraper" if scraper_enabled else "api"
    locked_mode = get_chat_mode(active_chat_id)
    if locked_mode and locked_mode != current_mode:
        return {
            "error": f"This chat was created in {locked_mode} mode. "
                     f"Switch back to {locked_mode} mode or start a new chat."
        }
    if scraper_enabled:
        current_provider = "scraping"
    elif _is_deepseek_api_model(request.model):
        current_provider = "deepseek"
    else:
        current_provider = "qwen"
    locked_provider = get_chat_provider(active_chat_id)
    if locked_provider and locked_provider != current_provider:
        return {
            "error": f"This chat is locked to {locked_provider}. "
                     f"You can't use {current_provider} here — start a new chat."
        }
    title = make_title(request.message)
    ensure_chat(active_chat_id, title, request.parent_id, mode=current_mode, provider=current_provider)
    set_title_if_default(active_chat_id, title)
    parent_id = get_parent_id(active_chat_id, request.parent_id)
    add_message(active_chat_id, "user", timestamped_message, None, parent_id, memory_used=_memory_used or None)
    resolved_files: list[dict[str, Any]] | None = None
    if request.files:
        resolved_files = []
        for f in request.files:
            if scraper_enabled:
                if "path" in f or "url" in f:
                    resolved_files.append(f)
                continue
            if "id" in f and "url" in f:
                resolved_files.append(f)
            elif "path" in f:
                meta = await service.upload_image(f["path"])
                if meta:
                    resolved_files.append(meta)
                else:
                    logger.warning("Could not resolve file: %s", f["path"])
    if not request.stream and scraper_enabled:
        result = await scraper_service.chat(
            message=timestamped_message,
            chat_id=active_chat_id,
            parent_id=parent_id,
            files=resolved_files,
            model=request.model,
            thinking_mode=request.thinking_mode,
        )
        answer = str(result.get("answer", ""))
        thinking = str(result.get("thinking", ""))
        final_parent = result.get("parent_id") or parent_id
        error = result.get("error")
        scraper_chat_url = result.get("chat_url")
        if scraper_chat_url:
            save_chat_url(active_chat_id, scraper_chat_url)
        add_message(active_chat_id, "assistant", answer or error or "", thinking, final_parent)
        touch_chat(active_chat_id, final_parent)
        result["memory_used"] = _memory_used
        return result
    if not request.stream and _is_deepseek_api_model(request.model):
        ds_cfg = get_model_config(request.model)
        api_model_type = ds_cfg.get("api_model_type", "default")
        ds_ephemeral = api_model_type == "vision" and bool(request.ref_file_ids)
        result = await get_deepseek_client().chat(
            message=timestamped_message,
            model=api_model_type,
            thinking_mode=request.thinking_mode,
            chat_id=None if ds_ephemeral else active_chat_id,
            ref_file_ids=request.ref_file_ids,
            inject_instructions=not ds_ephemeral,
        )
        answer = str(result.get("answer", ""))
        thinking = str(result.get("thinking", ""))
        final_parent = result.get("parent_id") or parent_id
        error = result.get("error")
        add_message(active_chat_id, "assistant", answer or error or "", thinking, final_parent)
        touch_chat(active_chat_id, final_parent)
        result["memory_used"] = _memory_used
        return result
    if not request.stream and not scraper_enabled:
        result = await retry_async(
            lambda: service.chat(
                message=timestamped_message,
                chat_id=active_chat_id,
                parent_id=parent_id,
                files=resolved_files,
                model=request.model,
                thinking_mode=request.thinking_mode,
            ),
            label="chat",
        )
        answer = str(result.get("answer", ""))
        thinking = str(result.get("thinking", ""))
        final_parent = result.get("parent_id") or parent_id
        error = result.get("error")
        add_message(active_chat_id, "assistant", answer or error or "", thinking, final_parent)
        touch_chat(active_chat_id, final_parent)
        result["memory_used"] = _memory_used
        return result
    async def event_stream():
        answer_parts: list[str] = []
        thinking_parts: list[str] = []
        skill_events: list[dict[str, Any]] = []
        final_parent = parent_id
        error_message: str | None = None
        current_message = timestamped_message
        current_parent = parent_id
        round_index = 0
        saved_message_id: int | None = None
        _all_tool_mem_used: list[dict[str, Any]] = []
        yield sse({"type": "status", "message": "processing"})
        if _memory_used:
            yield sse({"type": "memory_used", "memories": _memory_used})
        try:
            while True:
                round_skill_events: list[dict[str, Any]] = []
                round_thinking_parts: list[str] = []
                round_answer_parts: list[str] = []
                parser = SkillParser()
                def emit_parsed(text: str) -> Generator[str, None, None]:
                    for item in parser.feed(text):
                        if item.get("type") == "text":
                            chunk = str(item.get("text", ""))
                            if chunk:
                                answer_parts.append(chunk)
                                round_answer_parts.append(chunk)
                                yield sse({"type": "answer", "text": chunk})
                        else:
                            if item.get("type") in ("skill_start", "skill_output", "skill_end", "file_edit"):
                                round_skill_events.append(item)
                            yield sse(item)
                def emit_flush() -> Generator[str, None, None]:
                    for item in parser.flush():
                        if item.get("type") == "text":
                            chunk = str(item.get("text", ""))
                            if chunk:
                                answer_parts.append(chunk)
                                round_answer_parts.append(chunk)
                                yield sse({"type": "answer", "text": chunk})
                        else:
                            if item.get("type") in ("skill_start", "skill_output", "skill_end", "file_edit"):
                                round_skill_events.append(item)
                            yield sse(item)
                files_for_round = resolved_files if round_index == 0 else None
                stream_error = False
                if _is_deepseek_api_model(request.model):
                    _ds_cfg = get_model_config(request.model)
                    _ds_api_type = _ds_cfg.get("api_model_type", "default")
                    _ds_ephemeral = _ds_api_type == "vision" and bool(request.ref_file_ids)
                    round_event_source = get_deepseek_client().stream_chat(
                        message=current_message,
                        model=_ds_api_type,
                        thinking_mode=request.thinking_mode,
                        chat_id=None if _ds_ephemeral else active_chat_id,
                        ref_file_ids=request.ref_file_ids if round_index == 0 else None,
                        inject_instructions=not _ds_ephemeral,
                    )
                elif scraper_enabled:
                    round_event_source = scraper_service.stream_events(
                        message=current_message,
                        chat_id=active_chat_id,
                        parent_id=current_parent,
                        files=files_for_round,
                        model=request.model,
                        thinking_mode=request.thinking_mode,
                    )
                else:
                    round_event_source = retry_stream(
                        lambda: service.stream_events(
                            message=current_message,
                            chat_id=active_chat_id,
                            parent_id=current_parent,
                            files=files_for_round,
                            model=request.model,
                            thinking_mode=request.thinking_mode,
                        ),
                        label=f"stream_round_{round_index}",
                    )
                async for event in round_event_source:
                    event_type = event.get("type")
                    if event_type == "answer":
                        for _sse_line in emit_parsed(str(event.get("text", ""))):
                            yield _sse_line
                        continue
                    if event_type == "thinking":
                        thinking_parts.append(str(event.get("text", "")))
                        round_thinking_parts.append(str(event.get("text", "")))
                    elif event_type == "done":
                        for _sse_line in emit_flush():
                            yield _sse_line
                        final_parent = event.get("parent_id") or final_parent
                        current_parent = final_parent
                    elif event_type == "error":
                        for _sse_line in emit_flush():
                            yield _sse_line
                        error_message = str(event.get("message", "Unknown error"))
                        stream_error = True
                    elif event_type == "rate_limited":
                        for _sse_line in emit_flush():
                            yield _sse_line
                        hours = event.get("hours", "?")
                        details = event.get("message", "Daily usage limit reached.")
                        error_message = f"⏳ Rate Limited — {details} (retry in {hours}h)"
                        stream_error = True
                    yield sse(event)
                round_thinking_text = "".join(round_thinking_parts)
                if round_thinking_text:
                    skill_events.append({"type": "round_thinking", "text": round_thinking_text})
                round_text = "".join(round_answer_parts)
                if round_text:
                    skill_events.append({"type": "round_text", "text": round_text})
                if round_skill_events:
                    skill_events.extend(round_skill_events)
                round_answer = "".join(answer_parts)
                round_thinking = "".join(thinking_parts)
                stored = round_answer or error_message or ""
                if saved_message_id is None:
                    saved_message_id = add_message(
                        active_chat_id, "assistant", stored, round_thinking, final_parent, skill_events,
                        memory_used=_all_tool_mem_used or None,
                    )
                else:
                    update_message(saved_message_id, stored, round_thinking, final_parent, skill_events,
                                   memory_used=_all_tool_mem_used or None)
                feedback = build_tool_feedback(round_skill_events)
                if stream_error or error_message or not feedback:
                    break
                try:
                    _max_chars_tool = _ms_cfg.get("max_prompt_chars", _DEFAULT_MAX_PROMPT_CHARS)
                    if _ms_cfg.get("enabled", True) and feedback and len(feedback) <= _max_chars_tool:
                        _tool_mem = _searcher.search(feedback, top_k=_ms_cfg.get("top_k", 10))
                        _new_mem = [r for r in _tool_mem if r.get("key") and r["key"] not in _injected_memory_keys]
                        if _new_mem:
                            _tool_block = _searcher.format_for_prompt(_new_mem)
                            if _tool_block:
                                feedback = f"{_tool_block}\n\n{feedback}"
                                for r in _new_mem:
                                    _injected_memory_keys.add(r["key"])
                                save_injected_memory_keys(active_chat_id, _injected_memory_keys)
                                _tool_mem_used = [
                                    {
                                        "key": r.get("key", ""),
                                        "value": r.get("value", ""),
                                        "category": r.get("category", ""),
                                        "score": round(float(r.get("score", 0.0)), 3),
                                    }
                                    for r in _new_mem
                                ]
                                _all_tool_mem_used.extend(_tool_mem_used)
                                skill_events.append(
                                    {"type": "memory_used", "memories": _tool_mem_used, "round": round_index}
                                )
                                yield sse(
                                    {
                                        "type": "memory_used",
                                        "memories": _tool_mem_used,
                                        "source": "tool",
                                        "round": round_index,
                                    }
                                )
                except Exception:
                    pass
                if round_index >= SKILL_ROUND_WARN_THRESHOLD:
                    logger.warning(
                        "chat %s reached %d skill rounds — still running but worth checking",
                        active_chat_id, round_index,
                    )
                    yield sse({"type": "status", "message": "high_skill_round_count", "round": round_index})
                round_index += 1
                current_message = feedback
                current_parent = final_parent
                yield sse(
                    {
                        "type": "status",
                        "message": "feeding_skill_results",
                        "round": round_index,
                    }
                )
        except Exception as exc:
            error_message = f"{type(exc).__name__}: {exc}"
            try:
                yield sse({"type": "error", "message": f"Server error: {error_message}"})
            except Exception:
                pass
        finally:
            answer = "".join(answer_parts)
            thinking = "".join(thinking_parts)
            if not answer and skill_events:
                summary = []
                for evt in skill_events:
                    if evt.get("type") == "skill_start":
                        summary.append(f"[skill] {evt.get('name', 'skill')}")
                    elif evt.get("type") == "skill_end":
                        status = "ok" if evt.get("ok") else "error"
                        summary.append(f"[{status}] {evt.get('name', 'skill')}")
                answer = "\n".join(summary)
            stored_content = answer or error_message or ""
            if saved_message_id is not None:
                update_message(saved_message_id, stored_content, thinking, final_parent, skill_events)
            else:
                add_message(active_chat_id, "assistant", stored_content, thinking, final_parent, skill_events)
            touch_chat(active_chat_id, final_parent)
    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

@app.post("/api/deepseek/upload-file")
async def deepseek_upload_file(
    file: UploadFile = File(...),
    model_type: str = Query("vision"),
    thinking_enabled: bool = Query(False),
) -> dict[str, Any]:
    import uuid as _uuid
    from pathlib import Path as _Path
    suffix = _Path(file.filename or "image.png").suffix
    dest = UPLOAD_DIR / f"ds_{_uuid.uuid4().hex}{suffix}"
    content = await file.read()
    dest.write_bytes(content)
    try:
        meta = await service.upload_deepseek_file(
            str(dest),
            model_type=model_type,
            thinking_enabled=thinking_enabled,
        )
        return {
            "uploaded": True,
            "path": str(dest),
            "meta": {
                "file_id": meta.get("file_id"),
                "status": meta.get("status"),
                "file_name": meta.get("file_name"),
                "file_size": meta.get("file_size"),
                "model_kind": meta.get("model_kind"),
                "is_image": meta.get("is_image"),
            },
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"DeepSeek upload failed: {exc}")

@app.get("/", response_class=HTMLResponse)
def index() -> str:
    if INDEX_FILE.exists():
        return INDEX_FILE.read_text(encoding="utf-8")
    return "<h1>Sable API is running</h1><p>POST /api/chat</p>"