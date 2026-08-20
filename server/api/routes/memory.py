from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from engine.config import get_model_config
from engine.memory_search import get_searcher, list_available_models
from connectors.deepseek.client import get_client as get_deepseek_client
from connectors import get_connector
from instruction.mem_cmd import (
    _CONSOLIDATE_PROMPT_TEMPLATE_HISTORY,
    _CONSOLIDATE_PROMPT_TEMPLATE_STANDALONE,
    _MERGE_RESOLUTION_PROMPT,
    _PERSONALITY_ASSESSMENT_TEMPLATE,
)

from server.config import _MEMORY_PATH, _PROTECTED_PATH, _PROCEDURAL_PATH, _PERSONALITY_PATH, _PERSONAL_PATH, _MEMORY_SEARCH_SETTINGS, _DEFAULT_MAX_PROMPT_CHARS
from server.database import get_messages, get_injected_memory_keys, get_parent_id, get_chat_project_id, get_project
from server.utils import retry_async, _is_deepseek_api_model, _resolve_api_backend, logger
from ..dependencies import service
from engine.service import ChatService

router = APIRouter()

import re as _re
_MEM_BLOCK_RE = _re.compile(r'^\[RELEVANT MEMORY CONTEXT\][\s\S]*?\n\n')
_TS_RE = _re.compile(r'^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]\n?')

_CONSOLIDATION_BACKENDS = frozenset({"groq", "gemini", "mistral"})


def _format_conversation(messages: list[dict[str, Any]], max_chars: int = 50_000) -> str:
    """Format DB messages into a readable conversation block for consolidation."""
    parts: list[str] = []
    total = 0
    for i, msg in enumerate(messages):
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if not content:
            continue
        # Strip injected memory blocks and timestamp prefixes from user messages
        if role == "user":
            content = _MEM_BLOCK_RE.sub("", content)
            content = _TS_RE.sub("", content)
        label = "User" if role == "user" else "Assistant"
        line = f"[{label}]: {content}"
        if total + len(line) > max_chars:
            parts.append(f"...[{len(messages) - i} more messages truncated]")
            break
        parts.append(line)
        total += len(line)
    return "\n\n".join(parts)

@router.get("/api/settings/memory")
async def get_memory() -> dict[str, Any]:
    result: dict[str, list] = {"semantic": [], "episodic": [], "procedural": [], "ephemeral": []}
    # Load non-procedural from Memory.json
    if _MEMORY_PATH.exists():
        try:
            data = json.loads(_MEMORY_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for cat in ("semantic", "episodic", "ephemeral"):
                    result[cat] = data.get(cat, [])
        except Exception:
            pass
    # Load procedural from separate file
    if _PROCEDURAL_PATH.exists():
        try:
            pdata = json.loads(_PROCEDURAL_PATH.read_text(encoding="utf-8"))
            if isinstance(pdata, dict):
                result["procedural"] = pdata.get("procedural", [])
        except Exception:
            pass
    return {"memory": result}

@router.post("/api/settings/memory")
async def update_memory(payload: dict[str, Any]) -> dict[str, str]:
    memory = payload.get("memory")
    if memory is None:
        raise HTTPException(status_code=400, detail="Missing 'memory' field")
    # Split procedural into separate file
    proc_entries = memory.pop("procedural", [])
    _MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _MEMORY_PATH.write_text(json.dumps(memory, indent=2, ensure_ascii=False), encoding="utf-8")
    if isinstance(proc_entries, list):
        _PROCEDURAL_PATH.write_text(
            json.dumps({"procedural": proc_entries}, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    get_searcher().reload_memory()
    return {"status": "ok"}


@router.delete("/api/settings/memory")
async def delete_memory_entry(payload: dict[str, Any]) -> dict[str, str]:
    """Delete a single memory entry by category and key."""
    category = payload.get("category", "")
    key = payload.get("key", "")
    if not category or not key:
        raise HTTPException(status_code=400, detail="Missing 'category' or 'key'")

    # Procedural lives in separate file
    if category == "procedural":
        if not _PROCEDURAL_PATH.exists():
            raise HTTPException(status_code=404, detail="Procedural file not found")
        try:
            pdata = json.loads(_PROCEDURAL_PATH.read_text(encoding="utf-8"))
        except Exception:
            raise HTTPException(status_code=500, detail="Failed to read procedural file")
        entries = pdata.get("procedural", []) if isinstance(pdata, dict) else []
        original_len = len(entries)
        pdata["procedural"] = [e for e in entries if e.get("key") != key]
        if len(pdata["procedural"]) == original_len:
            raise HTTPException(status_code=404, detail=f"Entry with key '{key}' not found in 'procedural'")
        _PROCEDURAL_PATH.write_text(json.dumps(pdata, indent=2, ensure_ascii=False), encoding="utf-8")
        get_searcher().reload_memory()
        return {"status": "ok", "deleted": key}

    # Other categories in Memory.json
    if not _MEMORY_PATH.exists():
        raise HTTPException(status_code=404, detail="Memory file not found")
    try:
        data = json.loads(_MEMORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to read memory file")
    if not isinstance(data, dict) or category not in data:
        raise HTTPException(status_code=404, detail=f"Category '{category}' not found")
    entries = data[category]
    original_len = len(entries)
    data[category] = [e for e in entries if e.get("key") != key]
    if len(data[category]) == original_len:
        raise HTTPException(status_code=404, detail=f"Entry with key '{key}' not found in '{category}'")
    _MEMORY_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    get_searcher().reload_memory()
    return {"status": "ok", "deleted": key}


@router.delete("/api/settings/memory/protected")
async def delete_protected_entry(payload: dict[str, Any]) -> dict[str, str]:
    """Delete a single protected memory entry by key."""
    key = payload.get("key", "")
    if not key:
        raise HTTPException(status_code=400, detail="Missing 'key'")
    if not _PROTECTED_PATH.exists():
        raise HTTPException(status_code=404, detail="Protected memory file not found")
    try:
        data = json.loads(_PROTECTED_PATH.read_text(encoding="utf-8"))
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to read protected memory file")
    entries = data.get("protected", []) if isinstance(data, dict) else []
    original_len = len(entries)
    entries = [e for e in entries if e.get("key") != key]
    if len(entries) == original_len:
        raise HTTPException(status_code=404, detail=f"Protected entry with key '{key}' not found")
    _PROTECTED_PATH.write_text(
        json.dumps({"protected": entries}, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    get_searcher().reload_memory()
    return {"status": "ok", "deleted": key}


@router.get("/api/settings/memory/protected")
async def get_protected_memory() -> dict[str, Any]:
    if not _PROTECTED_PATH.exists():
        return {"protected": []}
    try:
        data = json.loads(_PROTECTED_PATH.read_text(encoding="utf-8"))
        entries = data.get("protected", []) if isinstance(data, dict) else []
        return {"protected": entries}
    except Exception:
        return {"protected": []}

@router.post("/api/settings/memory/protected")
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

@router.get("/api/settings/memory-search")
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
        "top_memory": cfg.get("top_memory", 5),
        "top_procedural": cfg.get("top_procedural", 3),
        "top_total": cfg.get("top_total", 9),
        "max_prompt_chars": cfg.get("max_prompt_chars", _DEFAULT_MAX_PROMPT_CHARS),
        "model_thresholds": searcher.get_custom_thresholds(),
        "current_model": searcher.model_name,
        "current_threshold": searcher.threshold,
        "available_models": list_available_models(),
    }

@router.post("/api/settings/memory-search")
async def update_memory_search_settings(payload: dict[str, Any]) -> dict[str, Any]:
    model = payload.get("model")
    top_k = payload.get("top_k")
    top_memory = payload.get("top_memory")
    top_procedural = payload.get("top_procedural")
    top_total = payload.get("top_total")
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
    if top_memory is not None:
        cfg["top_memory"] = max(1, int(top_memory))
    if top_procedural is not None:
        cfg["top_procedural"] = max(1, int(top_procedural))
    if top_total is not None:
        cfg["top_total"] = max(1, int(top_total))
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

@router.post("/api/settings/memory-search/refresh-cache")
async def refresh_memory_cache() -> dict[str, Any]:
    count = get_searcher().rebuild_cache()
    return {"status": "ok", "detail": f"Cache rebuilt. {count} entries re-embedded."}

def _save_user_skill(skill_data: dict[str, Any]) -> bool:
    """Persist a user-created skill to Brain/skills.json (same pattern as Protected.json)."""
    from engine.config import SKILLS_JSON_PATH
    try:
        existing: dict[str, list] = {"skills": []}
        if SKILLS_JSON_PATH.exists():
            existing = json.loads(SKILLS_JSON_PATH.read_text(encoding="utf-8"))
            if not isinstance(existing, dict) or "skills" not in existing:
                existing = {"skills": []}
        # Deduplicate by name
        names = {s.get("name") for s in existing["skills"] if isinstance(s, dict)}
        if skill_data["name"] in names:
            # Update existing
            existing["skills"] = [
                skill_data if s.get("name") == skill_data["name"] else s
                for s in existing["skills"]
            ]
        else:
            from datetime import datetime
            skill_data.setdefault("created", datetime.now().strftime("%Y-%m-%d"))
            existing["skills"].append(skill_data)
        SKILLS_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
        SKILLS_JSON_PATH.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
        return True
    except Exception:
        return False



@router.get("/api/settings/personal")
async def get_personal_preferences() -> dict[str, Any]:
    """Load user preferences from instruction/personal.md."""
    if not _PERSONAL_PATH.exists():
        return {"content": ""}
    try:
        return {"content": _PERSONAL_PATH.read_text(encoding="utf-8")}
    except Exception:
        return {"content": ""}

@router.post("/api/settings/personal")
async def update_personal_preferences(payload: dict[str, Any]) -> dict[str, str]:
    """Save user preferences to instruction/personal.md."""
    content = payload.get("content", "")
    _PERSONAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PERSONAL_PATH.write_text(content, encoding="utf-8")
    return {"status": "ok"}

def _dedup_and_resolve_adds(
    adds: dict[str, list[dict[str, str]]],
    existing: dict[str, list[dict[str, str]]],
    searcher,
) -> tuple[dict[str, list[dict[str, str]]], int, int]:
    """Post-consolidation dedup and contradiction resolution.
    
    Uses direct vector cosine similarity on key+value text.
    - If best match similarity >= 0.82: skip (duplicate)
    - If best match similarity >= 0.65 and < 0.82: update existing entry in-place
    - Otherwise: add as new
    
    Returns (filtered_adds, skipped_count, updated_count).
    """
    import numpy as np
    
    MERGE_THRESHOLD = 0.88
    UPDATE_THRESHOLD = 0.80
    
    skipped = 0
    updated = 0
    filtered: dict[str, list[dict[str, str]]] = {}
    
    # Build a single pool of ALL existing entries across categories for comparison
    all_existing: list[dict] = []
    for cat in ("semantic", "episodic", "procedural", "protected"):
        for e in existing.get(cat, []):
            if isinstance(e, dict) and e.get("key"):
                all_existing.append(e)
    
    # Embed all existing entries once
    existing_texts = [f"{e['key']}: {e.get('value', '')}" for e in all_existing]
    existing_vecs = None
    if existing_texts:
        try:
            existing_vecs = searcher._embed_texts(existing_texts)
        except Exception:
            existing_vecs = None
    
    for cat in ("semantic", "episodic", "procedural"):
        new_list = adds.get(cat, [])
        if not isinstance(new_list, list) or not new_list:
            filtered[cat] = []
            continue
            
        existing_list = existing.get(cat, [])
        
        kept: list[dict[str, str]] = []
        for entry in new_list:
            if not isinstance(entry, dict) or not entry.get("key"):
                continue
            
            entry_text = f"{entry['key']}: {entry.get('value', '')}"
            
            # Direct cosine similarity against all existing entries
            best_sim = 0.0
            best_idx = -1
            if existing_vecs is not None and len(existing_vecs) > 0:
                try:
                    new_vec = searcher._embed_texts([entry_text])
                    sims = (existing_vecs @ new_vec.T).flatten()
                    best_idx = int(np.argmax(sims))
                    best_sim = float(sims[best_idx])
                except Exception:
                    best_sim = 0.0
            
            if best_sim >= MERGE_THRESHOLD:
                matched_entry = all_existing[best_idx]
                has_retrieval = bool(entry.get("source_query") or entry.get("tags") or entry.get("triggers"))
                
                if has_retrieval and matched_entry.get("key") == entry["key"]:
                    # Same key with enriched fields — update in place
                    matched_entry["value"] = entry.get("value", matched_entry.get("value", ""))
                    if entry.get("source_query"):
                        matched_entry["source_query"] = str(entry["source_query"]).strip()
                    if entry.get("tags") and isinstance(entry["tags"], list):
                        matched_entry["tags"] = [str(t).lower().strip() for t in entry["tags"] if str(t).strip()]
                    if entry.get("triggers") and isinstance(entry["triggers"], list):
                        matched_entry["triggers"] = [str(t).strip() for t in entry["triggers"] if str(t).strip()]
                    updated += 1
                else:
                    skipped += 1
                continue
            elif best_sim >= UPDATE_THRESHOLD and best_idx >= 0:
                matched_entry = all_existing[best_idx]
                if matched_entry.get("key") != entry["key"]:
                    # Update existing entry's value + retrieval fields
                    matched_entry["value"] = entry.get("value", "")
                    if entry.get("source_query"):
                        matched_entry["source_query"] = str(entry["source_query"]).strip()
                    if entry.get("tags") and isinstance(entry["tags"], list):
                        matched_entry["tags"] = [str(t).lower().strip() for t in entry["tags"] if str(t).strip()]
                    if entry.get("triggers") and isinstance(entry["triggers"], list):
                        matched_entry["triggers"] = [str(t).strip() for t in entry["triggers"] if str(t).strip()]
                    updated += 1
                    continue
            
            kept_entry: dict[str, Any] = {"key": entry["key"], "value": entry.get("value", "")}
            if cat == "procedural":
                if entry.get("trigger"):
                    kept_entry["trigger"] = str(entry["trigger"])
                if entry.get("keywords") and isinstance(entry["keywords"], list):
                    kept_entry["keywords"] = [str(kw) for kw in entry["keywords"]]
            # Preserve retrieval fields (source_query + tags + triggers) for all categories
            if entry.get("source_query"):
                kept_entry["source_query"] = str(entry["source_query"]).strip()
            if entry.get("tags") and isinstance(entry["tags"], list):
                kept_entry["tags"] = [str(t).lower().strip() for t in entry["tags"] if str(t).strip()]
            if entry.get("triggers") and isinstance(entry["triggers"], list):
                kept_entry["triggers"] = [str(t).strip() for t in entry["triggers"] if str(t).strip()]
            kept.append(kept_entry)
        
        filtered[cat] = kept
    
    # Pass through protected and ephemeral unchanged
    filtered["protected"] = adds.get("protected", [])
    filtered["ephemeral"] = adds.get("ephemeral", [])
    
    return filtered, skipped, updated

def _load_consolidation_settings() -> dict[str, Any]:
    """Load consolidation settings from system/consolidation_settings.json."""
    defaults: dict[str, Any] = {"model": "", "fallback_models": [], "browser_profiles": []}
    path = Path(__file__).resolve().parent.parent.parent.parent / "system" / "consolidation_settings.json"
    if path.exists():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                defaults.update(stored)
        except Exception:
            pass
    return defaults

async def _try_consolidation_call(
    model: str, prompt: str, browser_profile: str = "", system_instruction: str = ""
) -> dict[str, Any] | None:
    """Try a single consolidation model call. Returns result dict or None on failure.
    
    Always uses chat_id=None and parent_id=None so the Qwen service creates a fresh
    server-side session automatically. Consolidation must NOT reuse the source
    conversation's chat_id (different browser profiles won't have it, and appending
    to the same chat pollutes history).

    For Qwen paths, uses a DEDICATED temp ChatService to avoid sharing the main
    chat's Playwright browser/lock.
    """
    try:
        api_backend = _resolve_api_backend(model)

        # ── Diagnostic print: what path is actually taken ──
        _log_path = "API" if api_backend else (f"Qwen@{browser_profile}" if browser_profile else "Qwen@default")
        _log_browser_dir = ""
        _log_waf_account = ""
        if not api_backend:
            from engine.config import _SYSTEM as _SYS, _resolve_active_account
            if browser_profile:
                _log_browser_dir = str(_SYS / browser_profile)
                _log_waf_account = browser_profile
            else:
                _log_browser_dir = str(_SYS / "browser-data")
                _log_waf_account = _resolve_active_account()
        print(
            f"\n{'='*60}\n"
            f"[CONSOLIDATION CALL]\n"
            f"  model:              {model}\n"
            f"  path:               {_log_path}\n"
            f"  api_backend:        {api_backend or '(none)'}\n"
            f"  browser_dir:        {_log_browser_dir or '(n/a)'}\n"
            f"  waf_account:        {_log_waf_account or '(n/a)'}\n"
            f"  system_instruction: {'YES (personality)' if system_instruction else 'NO (consolidation)'}\n"
            f"{'='*60}\n",
            flush=True,
        )

        if api_backend:
            # API-backed model (Gemini/Groq/Mistral/DeepSeek/etc.) — no Playwright
            if api_backend in _CONSOLIDATION_BACKENDS:
                connector = get_connector(api_backend)
                _extra_kwargs = {}
                if system_instruction:
                    _extra_kwargs["system_instruction"] = system_instruction
                result = await retry_async(
                    lambda: connector.chat(
                        message=prompt,
                        model=model,
                        inject_instructions=False,
                        chat_id=None,
                        **_extra_kwargs,
                    ),
                    label=f"memory_consolidate_{api_backend}",
                )
            elif _is_deepseek_api_model(model):
                ds_cfg = get_model_config(model)
                ds_api_type = ds_cfg.get("api_model_type")
                ds_msg = f"{system_instruction}\n\n{prompt}" if system_instruction else prompt
                result = await retry_async(
                    lambda: get_deepseek_client().chat(
                        message=ds_msg,
                        model=ds_api_type,
                        thinking_mode="fast",
                        chat_id=None,
                        inject_instructions=False,
                    ),
                    label="memory_consolidate_ds",
                )
            else:
                _api_kwargs = {}
                if system_instruction:
                    _api_kwargs["system_instruction"] = system_instruction
                result = await retry_async(
                    lambda: service.chat(
                        message=prompt,
                        chat_id=None,
                        parent_id=None,
                        model=model,
                        **_api_kwargs,
                    ),
                    label="memory_consolidate_api",
                )
        elif browser_profile:
            # Qwen with specific browser profile — dedicated temp service
            from engine.config import _SYSTEM
            acc_dir = _SYSTEM / browser_profile
            if not acc_dir.exists():
                return None
            qwen_msg = f"{system_instruction}\n\n{prompt}" if system_instruction else prompt
            temp_service = ChatService(user_data_dir=str(acc_dir))
            try:
                result = await retry_async(
                    lambda: temp_service.chat(
                        message=qwen_msg,
                        chat_id=None,
                        model=model,
                    ),
                    label=f"memory_consolidate_profile_{browser_profile}",
                )
            finally:
                await temp_service.close()
        else:
            # Default Qwen — use DEDICATED temp service (not the shared main-chat one)
            # to avoid Playwright browser/lock contention with the user's active chat.
            qwen_msg = f"{system_instruction}\n\n{prompt}" if system_instruction else prompt
            temp_service = ChatService()
            try:
                result = await retry_async(
                    lambda: temp_service.chat(
                        message=qwen_msg,
                        chat_id=None,
                        parent_id=None,
                        model=model,
                    ),
                    label="memory_consolidate_default",
                )
            finally:
                await temp_service.close()

        answer = result.get("answer", "").strip()
        if answer:
            return result
        return None
    except Exception:
        return None


def _try_consolidation_call_threaded(
    model: str, prompt: str, browser_profile: str = ""
) -> dict[str, Any] | None:
    """Run a Qwen/Playwright consolidation call in a dedicated thread with its own event loop.
    Only used for Qwen paths — API backends stay on the main loop (shared httpx clients)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            _try_consolidation_call(model, prompt, browser_profile=browser_profile)
        )
    except Exception:
        return None
    finally:
        loop.close()


async def _consolidation_llm_phase(
    model: str, prompt: str, fallback_models: list[str], browser_profiles: list[str]
) -> tuple[dict[str, Any] | None, list[str]]:
    """LLM call phase: API backends run on main loop; Qwen paths get thread isolation.
    
    API backends (Gemini/Groq/Mistral/DeepSeek) use shared httpx.AsyncClient instances
    bound to the main event loop — they MUST NOT be moved to a different loop.
    Qwen paths use Playwright — they get a dedicated thread + event loop to avoid
    blocking the main loop with heavy browser operations.
    """
    attempts: list[str] = []
    api_backend = _resolve_api_backend(model)

    # ── Primary model ──
    if api_backend:
        # API backend — run on main event loop (httpx clients bound here)
        result = await _try_consolidation_call(model, prompt)
        if result:
            attempts.append(f"{model} ✓")
            return result, attempts
        attempts.append(f"{model} ✗")
    elif browser_profiles:
        # Qwen with configured profiles — NEVER use default browser data
        for profile in browser_profiles[:3]:
            result = await asyncio.to_thread(_try_consolidation_call_threaded, model, prompt, profile)
            if result:
                attempts.append(f"{model}@{profile} ✓")
                return result, attempts
            attempts.append(f"{model}@{profile} ✗")
    else:
        # Qwen with no configured profiles — last resort default
        result = await asyncio.to_thread(_try_consolidation_call_threaded, model, prompt, "")
        if result:
            attempts.append(f"{model}@default ✓")
            return result, attempts
        attempts.append(f"{model}@default ✗")

    # ── Fallback models (may be API or Qwen) ──
    if fallback_models:
        for fb_model in fallback_models[:3]:
            fb_backend = _resolve_api_backend(fb_model)
            if fb_backend:
                result = await _try_consolidation_call(fb_model, prompt)
            else:
                result = await asyncio.to_thread(_try_consolidation_call_threaded, fb_model, prompt, "")
            if result:
                attempts.append(f"{fb_model} ✓")
                return result, attempts
            attempts.append(f"{fb_model} ✗")

    return None, attempts

async def _run_personality_assessment(conv_text: str, model: str) -> bool:
    """Run personality assessment on the conversation and save to Brain/user_personality.json.
    
    Returns True if assessment was saved, False otherwise.
    Non-blocking failure — consolidation should not fail if personality assessment fails.
    """
    try:
        # Build system instruction (template) and user message (conversation)
        # No previous personality passed — model assesses from conversation only
        system_instruction = _PERSONALITY_ASSESSMENT_TEMPLATE
        system_instruction = system_instruction.replace("<<PREVIOUS_PERSONALITY>>", "(none)")

        # Conversation goes as user message
        prompt = f"CONVERSATION DATA:\n{conv_text}"

        result = await _try_consolidation_call(model, prompt, system_instruction=system_instruction)
        if not result:
            return False

        raw = result.get("answer", "").strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            lines = raw.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            raw = "\n".join(lines).strip()

        # Parse JSON
        try:
            personality = json.loads(raw)
        except json.JSONDecodeError:
            start = raw.find("{")
            end = raw.rfind("}")
            if start != -1 and end > start:
                try:
                    personality = json.loads(raw[start:end + 1])
                except json.JSONDecodeError:
                    return False
            else:
                return False

        if not isinstance(personality, dict):
            return False

        # Validate minimal structure
        required_keys = {"strengths", "weaknesses", "contradictions", "blind_spots", "summary"}
        if not required_keys.issubset(personality.keys()):
            return False

        # Skip write if model says insufficient data (protects existing entries)
        if "insufficient data" in str(personality.get("summary", "")).lower():
            return False

        # Programmatic merge: preserve existing entries, add new ones
        _IDENT_FIELD = {
            "strengths": "trait",
            "weaknesses": "trait",
            "contradictions": "claimed",
            "blind_spots": "pattern",
        }
        if previous:
            try:
                prev_data = json.loads(previous)
            except (json.JSONDecodeError, TypeError):
                prev_data = {}
        else:
            prev_data = {}

        if isinstance(prev_data, dict):
            for cat, id_field in _IDENT_FIELD.items():
                existing_entries = prev_data.get(cat, [])
                new_entries = personality.get(cat, [])
                if not isinstance(existing_entries, list):
                    existing_entries = []
                if not isinstance(new_entries, list):
                    new_entries = []
                # Build set of identifiers from new entries for dedup
                new_ids = {
                    str(e.get(id_field, "")).lower().strip()
                    for e in new_entries if isinstance(e, dict) and e.get(id_field)
                }
                # Keep existing entries not contradicted/removed by new assessment
                merged = [
                    e for e in existing_entries
                    if isinstance(e, dict)
                    and str(e.get(id_field, "")).lower().strip() not in new_ids
                ]
                # Append all new entries
                merged.extend(e for e in new_entries if isinstance(e, dict))
                personality[cat] = merged

        # Save merged result
        _PERSONALITY_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PERSONALITY_PATH.write_text(
            json.dumps(personality, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return True
    except Exception:
        return False


@router.get("/api/settings/personality")
async def get_personality() -> dict[str, Any]:
    """Load user personality assessment from Brain/user_personality.json."""
    if not _PERSONALITY_PATH.exists():
        return {"personality": None}
    try:
        data = json.loads(_PERSONALITY_PATH.read_text(encoding="utf-8"))
        return {"personality": data}
    except Exception:
        return {"personality": None}


def _consolidation_prep(chat_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Synchronous prep phase: load messages, settings, build prompt.
    Runs in a thread via asyncio.to_thread() to avoid blocking the event loop."""
    _mem_path = _MEMORY_PATH
    _proj_id = None
    try:
        _proj_id = get_chat_project_id(chat_id)
        if _proj_id:
            _proj = get_project(_proj_id)
            if _proj and _proj.get("project_memory_enabled"):
                _proj_mem_dir = Path(__file__).resolve().parent.parent.parent / "system" / "projects" / _proj_id
                _proj_mem_dir.mkdir(parents=True, exist_ok=True)
                _mem_path = _proj_mem_dir / "Memory.json"
    except Exception:
        pass

    messages = get_messages(chat_id)
    if len(messages) < 2:
        return {"skip": True}

    settings = _load_consolidation_settings()
    model = settings.get("model") or payload.get("model") or ""
    if not model:
        return {"error": "No model specified"}

    fallback_models: list[str] = settings.get("fallback_models", [])
    browser_profiles: list[str] = settings.get("browser_profiles", [])

    injected_keys = get_injected_memory_keys(chat_id)
    filtered_memory: dict[str, list] = {}
    if _mem_path.exists() and injected_keys:
        try:
            full_memory = json.loads(_mem_path.read_text(encoding="utf-8"))
            if isinstance(full_memory, dict):
                for category, entries in full_memory.items():
                    if isinstance(entries, list):
                        matched = [e for e in entries if isinstance(e, dict) and e.get("key") in injected_keys]
                        if matched:
                            filtered_memory[category] = matched
        except Exception:
            pass
    current_memory = json.dumps(filtered_memory, indent=2) if filtered_memory else "{}"

    prompt = _CONSOLIDATE_PROMPT_TEMPLATE_STANDALONE
    prompt = prompt.replace("<<CURRENT_MEMORY>>", current_memory)
    conv_text = _format_conversation(messages)
    prompt = prompt.replace("<<CONVERSATION_SUMMARY>>", conv_text)

    return {
        "mem_path": _mem_path,
        "proj_id": _proj_id,
        "model": model,
        "fallback_models": fallback_models,
        "browser_profiles": browser_profiles,
        "prompt": prompt,
        "conv_text": conv_text,
    }


# Merge resolution is handled by _resolve_candidates_against_memory from
# engine.agents.memory_trigger (imported at call sites).


def _consolidation_apply(
    raw_answer: str, mem_path: Path, proj_id: str | None, new_entries: dict[str, Any]
) -> dict[str, Any]:
    """Synchronous apply phase: parse LLM response, dedup, merge, write memory files.
    Runs in a thread via asyncio.to_thread() to avoid blocking the event loop."""
    adds = new_entries.get("add") if "add" in new_entries else {
        k: v for k, v in new_entries.items()
        if k in ("semantic", "episodic", "procedural", "protected", "ephemeral")
    }
    deletes = new_entries.get("delete", []) if "add" in new_entries else []
    if not isinstance(adds, dict):
        adds = {}
    if not isinstance(deletes, list):
        deletes = []

    existing: dict[str, list[dict[str, str]]] = {}
    if mem_path.exists():
        try:
            existing = json.loads(mem_path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except Exception:
            existing = {}
    # Strip any leftover procedural from Memory.json (lives in Procedural.json now)
    existing.pop("procedural", None)
    # Load procedural from separate file for dedup
    proc_path = mem_path.parent / "Procedural.json"
    if proc_path.exists():
        try:
            pdata = json.loads(proc_path.read_text(encoding="utf-8"))
            if isinstance(pdata, dict):
                existing["procedural"] = pdata.get("procedural", [])
        except Exception:
            pass

    # Dedup disabled — the LLM already sees existing memory and decides what to add.
    # Post-hoc vector similarity was overriding correct model decisions.
    dedup_skipped = 0
    dedup_updated = 0

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
        for cat in ("semantic", "episodic", "ephemeral"):
            cat_list = existing.get(cat, [])
            before = len(cat_list)
            existing[cat] = [e for e in cat_list if not isinstance(e, dict) or e.get("key", "") not in delete_keys]
            deleted_count += before - len(existing[cat])
        # Also delete from procedural file
        if _PROCEDURAL_PATH.exists():
            try:
                proc_data = json.loads(_PROCEDURAL_PATH.read_text(encoding="utf-8"))
                if isinstance(proc_data, dict):
                    proc_list = proc_data.get("procedural", [])
                    before = len(proc_list)
                    proc_data["procedural"] = [e for e in proc_list if not isinstance(e, dict) or e.get("key", "") not in delete_keys]
                    deleted_count += before - len(proc_data["procedural"])
                    _PROCEDURAL_PATH.write_text(json.dumps(proc_data, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass

    added_count = 0
    # Non-procedural categories go to Memory.json
    for cat in ("semantic", "episodic"):
        existing_list = existing.get(cat, [])
        new_list = adds.get(cat, [])
        if not isinstance(new_list, list):
            continue
        existing_keys = {e.get("key", "") for e in existing_list if isinstance(e, dict)}
        for entry in new_list:
            if isinstance(entry, dict) and entry.get("key") and entry["key"] not in existing_keys:
                new_entry: dict[str, Any] = {"key": entry["key"], "value": entry.get("value", "")}
                # Preserve retrieval fields (source_query + tags + triggers)
                if entry.get("source_query"):
                    new_entry["source_query"] = str(entry["source_query"]).strip()
                if entry.get("tags") and isinstance(entry["tags"], list):
                    new_entry["tags"] = [str(t).lower().strip() for t in entry["tags"] if str(t).strip()]
                if entry.get("triggers") and isinstance(entry["triggers"], list):
                    new_entry["triggers"] = [str(t).strip() for t in entry["triggers"] if str(t).strip()]
                existing_list.append(new_entry)
                existing_keys.add(entry["key"])
                added_count += 1
        existing[cat] = existing_list

    # Procedural entries go to separate Procedural.json
    proc_new = adds.get("procedural", [])
    proc_added = 0
    if isinstance(proc_new, list) and proc_new:
        proc_existing: list[dict[str, Any]] = []
        if _PROCEDURAL_PATH.exists():
            try:
                pdata = json.loads(_PROCEDURAL_PATH.read_text(encoding="utf-8"))
                proc_existing = pdata.get("procedural", []) if isinstance(pdata, dict) else []
            except Exception:
                proc_existing = []
        proc_keys = {e.get("key", "") for e in proc_existing if isinstance(e, dict)}
        for entry in proc_new:
            if isinstance(entry, dict) and entry.get("key") and entry["key"] not in proc_keys:
                new_entry: dict[str, Any] = {"key": entry["key"], "value": entry.get("value", "")}
                if entry.get("trigger"):
                    new_entry["trigger"] = str(entry["trigger"])
                if entry.get("keywords") and isinstance(entry["keywords"], list):
                    new_entry["keywords"] = [str(kw) for kw in entry["keywords"]]
                # Preserve retrieval fields (source_query + tags + triggers)
                if entry.get("source_query"):
                    new_entry["source_query"] = str(entry["source_query"]).strip()
                if entry.get("tags") and isinstance(entry["tags"], list):
                    new_entry["tags"] = [str(t).lower().strip() for t in entry["tags"] if str(t).strip()]
                if entry.get("triggers") and isinstance(entry["triggers"], list):
                    new_entry["triggers"] = [str(t).strip() for t in entry["triggers"] if str(t).strip()]
                proc_existing.append(new_entry)
                proc_keys.add(entry["key"])
                proc_added += 1
                added_count += 1
        _PROCEDURAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PROCEDURAL_PATH.write_text(
            json.dumps({"procedural": proc_existing}, indent=2, ensure_ascii=False), encoding="utf-8"
        )

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
                # Preserve retrieval fields (source_query + tags + triggers)
                if entry.get("source_query"):
                    eph_entry["source_query"] = str(entry["source_query"]).strip()
                if entry.get("tags") and isinstance(entry["tags"], list):
                    eph_entry["tags"] = [str(t).lower().strip() for t in entry["tags"] if str(t).strip()]
                if entry.get("triggers") and isinstance(entry["triggers"], list):
                    eph_entry["triggers"] = [str(t).strip() for t in entry["triggers"] if str(t).strip()]
                eph_list.append(eph_entry)
                eph_keys.add(entry["key"])
                eph_added += 1
        existing["ephemeral"] = eph_list

    mem_path.parent.mkdir(parents=True, exist_ok=True)
    mem_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")

    if mem_path != _MEMORY_PATH:
        try:
            from engine.memory_search import reload_project_searcher
            reload_project_searcher(proj_id)
        except Exception:
            pass
    else:
        get_searcher().reload_memory()

    # Handle optional skill creation
    skill_created = False
    skill_data = new_entries.get("create_skill")
    if isinstance(skill_data, dict) and skill_data.get("name"):
        skill_created = _save_user_skill(skill_data)

    total_added = added_count + prot_added + eph_added
    result: dict[str, Any] = {
        "status": "ok",
        "added": total_added,
        "deleted": deleted_count,
        "dedup_skipped": dedup_skipped,
        "dedup_updated": dedup_updated,
    }
    if skill_created:
        result["skill_created"] = skill_data["name"]
    return result


@router.post("/api/memory/consolidate")
async def consolidate_memory(payload: dict[str, Any]) -> dict[str, Any]:
    chat_id = payload.get("chat_id")
    if not chat_id:
        raise HTTPException(status_code=400, detail="Missing 'chat_id'")

    # ── Prep phase in thread (DB reads, file I/O, formatting) ──
    prep = await asyncio.to_thread(_consolidation_prep, chat_id, payload)
    if prep is None:
        return {"status": "error", "detail": "Prep failed"}
    if prep.get("skip"):
        return {"status": "skipped", "reason": "too few messages"}
    if prep.get("error"):
        return {"status": "error", "detail": prep["error"]}

    _mem_path = prep["mem_path"]
    _proj_id = prep["proj_id"]
    model = prep["model"]
    prompt = prep["prompt"]
    conv_text = prep["conv_text"]

    # ── Log initial consolidation prompt ──
    try:
        from engine.agents.memory_trigger import _log_consolidation
        _log_consolidation("STEP 1 — MAIN CONSOLIDATION PROMPT", prompt)
    except Exception:
        pass

    # ── LLM call phase: API backends on main loop, Qwen in dedicated threads ──
    result, attempts = await _consolidation_llm_phase(
        model, prompt, prep["fallback_models"], prep["browser_profiles"]
    )

    if not result:
        try:
            _log_consolidation("STEP 1 — MAIN CONSOLIDATION LLM FAILED", f"All attempts failed: {' → '.join(attempts)}")
        except Exception:
            pass
        return {"status": "error", "detail": f"All consolidation attempts failed: {' → '.join(attempts)}"}

    # ── Parse LLM response (lightweight, stays on event loop) ──
    raw_answer = str(result.get("answer", ""))
    try:
        _log_consolidation("STEP 1 — MAIN CONSOLIDATION LLM OUTPUT", raw_answer)
    except Exception:
        pass
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

    try:
        _log_consolidation("STEP 1 — MAIN CONSOLIDATION PARSED", json.dumps(new_entries, indent=2, ensure_ascii=False))
    except Exception:
        pass

    # ── Multi-step merge resolution (async, before apply) ──
    try:
        from engine.agents.memory_trigger import _resolve_candidates_against_memory
        new_entries = await _resolve_candidates_against_memory(new_entries, model, [])
    except Exception as exc:
        logger.warning("[consolidation] Merge resolution failed, proceeding with original: %s", exc)

    try:
        _log_consolidation("STEP 2 — AFTER MERGE RESOLUTION", json.dumps(new_entries, indent=2, ensure_ascii=False))
    except Exception:
        pass

    # ── Apply phase in thread (dedup, merge, file writes, searcher reload) ──
    apply_result = await asyncio.to_thread(_consolidation_apply, raw_answer, _mem_path, _proj_id, new_entries)

    # Fire personality assessment in background — non-blocking, won't delay response
    asyncio.create_task(_run_personality_assessment(conv_text, model))

    return apply_result

def _consolidation_scraper_prep(chat_id: str) -> dict[str, Any] | None:
    """Synchronous prep for scraper consolidation. Runs in a thread."""
    messages = get_messages(chat_id)
    if len(messages) < 2:
        return {"skip": True}
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
    return {"messages": messages, "prompt": prompt}


@router.post("/api/memory/consolidate-scraper")
async def consolidate_memory_scraper(payload: dict[str, Any]) -> dict[str, Any]:
    chat_id = payload.get("chat_id")
    model = payload.get("model")
    if not chat_id:
        raise HTTPException(status_code=400, detail="Missing 'chat_id'")

    # ── Prep in thread ──
    prep = await asyncio.to_thread(_consolidation_scraper_prep, chat_id)
    if prep is None:
        return {"status": "error", "detail": "Prep failed"}
    if prep.get("skip"):
        return {"status": "skipped", "reason": "too few messages"}

    # ── Async scraper stream (non-blocking) ──
    answer_parts: list[str] = []
    error_msg: str | None = None
    try:
        from engine.scraper import scraper as scraper_service
        async for event in scraper_service.stream_events(
            message=prep["prompt"],
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

    # ── Parse LLM response (lightweight) ──
    raw_answer = "".join(answer_parts)
    try:
        from engine.agents.memory_trigger import _log_consolidation
        _log_consolidation("STEP 1 — SCRAPER CONSOLIDATION PROMPT", prep.get("prompt", ""))
        _log_consolidation("STEP 1 — SCRAPER CONSOLIDATION LLM OUTPUT", raw_answer)
    except Exception:
        pass
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

    try:
        _log_consolidation("STEP 1 — SCRAPER CONSOLIDATION PARSED", json.dumps(new_entries, indent=2, ensure_ascii=False))
    except Exception:
        pass

    # ── Multi-step merge resolution (async, before apply) ──
    try:
        from engine.agents.memory_trigger import _resolve_candidates_against_memory
        new_entries = await _resolve_candidates_against_memory(new_entries, model, [])
    except Exception as exc:
        logger.warning("[scraper-consolidation] Merge resolution failed, proceeding with original: %s", exc)

    try:
        _log_consolidation("STEP 2 — SCRAPER AFTER MERGE RESOLUTION", json.dumps(new_entries, indent=2, ensure_ascii=False))
    except Exception:
        pass

    # ── Apply in thread (reuses shared helper) ──
    apply_result = await asyncio.to_thread(_consolidation_apply, raw_answer, _MEMORY_PATH, None, new_entries)

    # Fire personality assessment in background — non-blocking, won't delay response
    conv_text = _format_conversation(prep["messages"])
    asyncio.create_task(_run_personality_assessment(conv_text, model))

    return apply_result