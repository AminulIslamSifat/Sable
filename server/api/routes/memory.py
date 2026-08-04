from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException
from engine.config import get_model_config
from engine.memory_search import get_searcher, list_available_models
from connectors.deepseek.client import get_client as get_deepseek_client
from connectors import get_connector
from instruction.mem_cmd import _CONSOLIDATE_PROMPT_TEMPLATE_HISTORY, _CONSOLIDATE_PROMPT_TEMPLATE_STANDALONE

from server.config import _MEMORY_PATH, _PROTECTED_PATH, _PERSONAL_PATH, _MEMORY_SEARCH_SETTINGS, _DEFAULT_MAX_PROMPT_CHARS
from server.database import get_messages, get_injected_memory_keys, get_parent_id
from server.utils import retry_async, _is_deepseek_api_model, _resolve_api_backend, logger
from ..dependencies import service

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

@router.post("/api/memory/consolidate")
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
        backend = _resolve_api_backend(model)
        try:
            if backend in _CONSOLIDATION_BACKENDS:
                # Groq / Gemini / Mistral: pass full conversation inline, one-shot call
                conv_text = _format_conversation(messages)
                prompt = prompt.replace("<<CONVERSATION_SUMMARY>>", conv_text)
                connector = get_connector(backend)
                result = await retry_async(
                    lambda: connector.chat(
                        message=prompt,
                        model=model,
                        inject_instructions=False,
                        chat_id=None,
                    ),
                    label=f"memory_consolidate_{backend}",
                )
            elif _is_deepseek_api_model(model):
                prompt = prompt.replace("<<CONVERSATION_SUMMARY>>", "(See conversation thread above — do not request more context.)")
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
                prompt = prompt.replace("<<CONVERSATION_SUMMARY>>", "(See conversation thread above — do not request more context.)")
                parent_id = get_parent_id(chat_id, None)
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
    # Handle optional skill creation
    skill_created = False
    skill_data = new_entries.get("create_skill")
    if isinstance(skill_data, dict) and skill_data.get("name"):
        skill_created = _save_user_skill(skill_data)
    total_added = added_count + prot_added + eph_added
    result: dict[str, Any] = {"status": "ok", "added": total_added, "deleted": deleted_count}
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