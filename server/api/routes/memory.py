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
    _PERSONALITY_ASSESSMENT_TEMPLATE,
)

from server.config import _MEMORY_PATH, _PROTECTED_PATH, _PERSONALITY_PATH, _PERSONAL_PATH, _MEMORY_SEARCH_SETTINGS, _DEFAULT_MAX_PROMPT_CHARS
from server.database import get_messages, get_injected_memory_keys, get_parent_id
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

@router.post("/api/settings/memory")
async def update_memory(payload: dict[str, Any]) -> dict[str, str]:
    memory = payload.get("memory")
    if memory is None:
        raise HTTPException(status_code=400, detail="Missing 'memory' field")
    _MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _MEMORY_PATH.write_text(json.dumps(memory, indent=2, ensure_ascii=False), encoding="utf-8")
    get_searcher().reload_memory()
    return {"status": "ok"}

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
    
    For each new entry in semantic/episodic/procedural:
    - Embed it and search against existing memory
    - If best match similarity >= 0.85: skip (duplicate)
    - If best match similarity >= 0.70 and < 0.85: update existing entry in-place (contradiction/update)
    - Otherwise: add as new
    
    Returns (filtered_adds, skipped_count, updated_count).
    """
    from engine.memory_search import MemorySearcher
    
    MERGE_THRESHOLD = 0.85
    UPDATE_THRESHOLD = 0.70
    
    skipped = 0
    updated = 0
    filtered: dict[str, list[dict[str, str]]] = {}
    
    for cat in ("semantic", "episodic", "procedural"):
        new_list = adds.get(cat, [])
        if not isinstance(new_list, list) or not new_list:
            filtered[cat] = []
            continue
            
        existing_list = existing.get(cat, [])
        existing_texts = []
        for e in existing_list:
            if isinstance(e, dict):
                k = str(e.get("key", "")).strip()
                v = str(e.get("value", "")).strip()
                existing_texts.append(f"{k}: {v}" if k else v)
        
        kept: list[dict[str, str]] = []
        for entry in new_list:
            if not isinstance(entry, dict) or not entry.get("key"):
                continue
            
            entry_text = f"{entry['key']}: {entry.get('value', '')}"
            
            # Search against ALL existing memory (not just same category)
            results = searcher.search(entry_text, top_k=3)
            
            if results:
                best_score = results[0]["score"]
                best_key = results[0]["key"]
                
                if best_score >= MERGE_THRESHOLD:
                    # Duplicate — skip
                    skipped += 1
                    continue
                elif best_score >= UPDATE_THRESHOLD and best_key != entry["key"]:
                    # Potential contradiction/update — replace existing entry's value
                    for e in existing_list:
                        if isinstance(e, dict) and e.get("key") == best_key:
                            e["value"] = entry.get("value", "")
                            updated += 1
                            break
                    continue
            
            kept.append({"key": entry["key"], "value": entry.get("value", "")})
        
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
    """
    try:
        api_backend = _resolve_api_backend(model)

        if api_backend:
            # API-backed model (Gemini/Groq/Mistral/DeepSeek/etc.)
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
                # DeepSeek chat() has no kwargs — prepend system instruction
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
            # Qwen with specific browser profile — no system_instruction support, prepend
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
            # Default Qwen service — no system_instruction support, prepend
            qwen_msg = f"{system_instruction}\n\n{prompt}" if system_instruction else prompt
            result = await retry_async(
                lambda: service.chat(
                    message=qwen_msg,
                    chat_id=None,
                    parent_id=None,
                    model=model,
                ),
                label="memory_consolidate_default",
            )

        answer = result.get("answer", "").strip()
        if answer:
            return result
        return None
    except Exception:
        return None

async def _run_personality_assessment(conv_text: str, model: str) -> bool:
    """Run personality assessment on the conversation and save to Brain/user_personality.json.
    
    Returns True if assessment was saved, False otherwise.
    Non-blocking failure — consolidation should not fail if personality assessment fails.
    """
    try:
        # Load previous assessment if exists
        previous = ""
        if _PERSONALITY_PATH.exists():
            try:
                previous = _PERSONALITY_PATH.read_text(encoding="utf-8")
            except Exception:
                previous = ""

        # Build system instruction (template) and user message (conversation)
        system_instruction = _PERSONALITY_ASSESSMENT_TEMPLATE
        system_instruction = system_instruction.replace("<<PREVIOUS_PERSONALITY>>", previous or "(none)")

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

        # Save
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


@router.post("/api/memory/consolidate")
async def consolidate_memory(payload: dict[str, Any]) -> dict[str, Any]:
    chat_id = payload.get("chat_id")
    if not chat_id:
        raise HTTPException(status_code=400, detail="Missing 'chat_id'")

    messages = get_messages(chat_id)
    if len(messages) < 2:
        return {"status": "skipped", "reason": "too few messages"}

    # Load consolidation settings
    settings = _load_consolidation_settings()
    model = settings.get("model") or payload.get("model") or ""
    if not model:
        return {"status": "error", "detail": "No model specified"}

    fallback_models: list[str] = settings.get("fallback_models", [])
    browser_profiles: list[str] = settings.get("browser_profiles", [])

    # Build memory context
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

    # Always use standalone template with inline conversation
    prompt = _CONSOLIDATE_PROMPT_TEMPLATE_STANDALONE
    prompt = prompt.replace("<<CURRENT_MEMORY>>", current_memory)
    conv_text = _format_conversation(messages)
    prompt = prompt.replace("<<CONVERSATION_SUMMARY>>", conv_text)

    # Fallback chain
    result: dict[str, Any] | None = None
    attempts: list[str] = []

    # 1. Try primary model
    result = await _try_consolidation_call(model, prompt)
    if result:
        attempts.append(f"{model} ✓")
    else:
        attempts.append(f"{model} ✗")

        # 2. Determine fallback strategy based on primary model type
        api_backend = _resolve_api_backend(model)

        if not api_backend and browser_profiles:
            # Qwen failed → try browser profiles
            for profile in browser_profiles[:3]:
                result = await _try_consolidation_call(model, prompt, browser_profile=profile)
                if result:
                    attempts.append(f"{model}@{profile} ✓")
                    break
                attempts.append(f"{model}@{profile} ✗")

        if not result and fallback_models:
            # Try fallback models (API or Qwen)
            for fb_model in fallback_models[:3]:
                result = await _try_consolidation_call(fb_model, prompt)
                if result:
                    attempts.append(f"{fb_model} ✓")
                    break
                attempts.append(f"{fb_model} ✗")

    if not result:
        return {"status": "error", "detail": f"All consolidation attempts failed: {' → '.join(attempts)}"}

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
    # Post-consolidation dedup and contradiction resolution
    try:
        searcher = get_searcher()
        adds, dedup_skipped, dedup_updated = _dedup_and_resolve_adds(adds, existing, searcher)
    except Exception:
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
    # Handle optional skill creation
    skill_created = False
    skill_data = new_entries.get("create_skill")
    if isinstance(skill_data, dict) and skill_data.get("name"):
        skill_created = _save_user_skill(skill_data)
    total_added = added_count + prot_added + eph_added

    # Fire personality assessment in background — non-blocking, won't delay response
    asyncio.create_task(_run_personality_assessment(conv_text, model))

    result: dict[str, Any] = {"status": "ok", "added": total_added, "deleted": deleted_count, "dedup_skipped": dedup_skipped, "dedup_updated": dedup_updated}
    if skill_created:
        result["skill_created"] = skill_data["name"]
    return result

@router.post("/api/memory/consolidate-scraper")
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
        from engine.scraper import scraper as scraper_service
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
    # Post-consolidation dedup and contradiction resolution
    try:
        searcher = get_searcher()
        adds, dedup_skipped, dedup_updated = _dedup_and_resolve_adds(adds, existing, searcher)
    except Exception:
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

    # Fire personality assessment in background
    conv_text = _format_conversation(messages)
    asyncio.create_task(_run_personality_assessment(conv_text, model))

    return {"status": "ok", "added": total_added, "deleted": deleted_count, "dedup_skipped": dedup_skipped, "dedup_updated": dedup_updated}