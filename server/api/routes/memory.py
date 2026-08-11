from __future__ import annotations

import asyncio
import json
import threading
import time
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
    _DEDUP_REVIEW_PROMPT_TEMPLATE,
    _SIMILARITY_CONTEXT_HEADER,
    _PERSONALITY_ASSESSMENT_TEMPLATE,
)

from server.config import _MEMORY_PATH, _PROTECTED_PATH, _PERSONALITY_PATH, _PERSONAL_PATH, _MEMORY_SEARCH_SETTINGS, _DEFAULT_MAX_PROMPT_CHARS
from server.database import get_messages, get_injected_memory_keys, get_parent_id, get_chat_project_id, get_project
from server.utils import retry_async, _is_deepseek_api_model, _resolve_api_backend, logger
from ..dependencies import service
from engine.service import ChatService

router = APIRouter()

import re as _re
_MEM_BLOCK_RE = _re.compile(r'^\[RELEVANT MEMORY CONTEXT\][\s\S]*?\n\n')
_TS_RE = _re.compile(r'^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]\n?')

_CONSOLIDATION_BACKENDS = frozenset({"groq", "gemini", "mistral"})

# ── Background consolidation infrastructure ─────────────────────────────
_consolidation_results: dict[str, dict[str, Any]] = {}
_consolidation_lock = threading.Lock()




def _build_stored_entry(entry: dict, category: str) -> dict:
    """Build stored memory entry. For procedural: preserve keyword/trigger, inject date."""
    stored: dict[str, Any] = {"key": entry["key"], "value": entry.get("value", "")}
    if category == "procedural":
        if entry.get("keyword"):
            stored["keyword"] = entry["keyword"]
        if entry.get("trigger"):
            stored["trigger"] = entry["trigger"]
        from datetime import date as _date
        stored["date"] = _date.today().isoformat()
    return stored



def _store_consolidation_result(chat_id: str, result: dict[str, Any]) -> None:
    """Store consolidation result for polling."""
    with _consolidation_lock:
        _consolidation_results[chat_id] = {**result, "_ts": time.time()}


def _get_consolidation_result(chat_id: str) -> dict[str, Any] | None:
    """Retrieve and remove consolidation result."""
    with _consolidation_lock:
        entry = _consolidation_results.pop(chat_id, None)
    if entry:
        entry.pop("_ts", None)
    return entry


def _run_consolidation_in_thread(
    chat_id: str, mem_path: Path, messages: list[dict], model: str,
    fallback_models: list[str], browser_profiles: list[str],
    current_memory: str, conv_text: str, proj_id: str | None,
) -> None:
    """Run full consolidation work in a separate thread with its own event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_do_consolidation_work(
            chat_id, mem_path, messages, model, fallback_models,
            browser_profiles, current_memory, conv_text, proj_id,
        ))
    except Exception as exc:
        _store_consolidation_result(chat_id, {"status": "error", "detail": f"Thread error: {exc}"})
    finally:
        loop.close()


async def _do_consolidation_work(
    chat_id: str, mem_path: Path, messages: list[dict], model: str,
    fallback_models: list[str], browser_profiles: list[str],
    current_memory: str, conv_text: str, proj_id: str | None,
) -> None:
    """Actual consolidation logic — runs inside the background thread's event loop."""
    # Build similarity context
    similarity_context = ""
    try:
        searcher = get_searcher()
        sim_results = searcher.search(conv_text[:3000], top_k=10)
        if sim_results:
            lines = [_SIMILARITY_CONTEXT_HEADER]
            for r in sim_results:
                if r.get("score", 0) >= 0.5:
                    lines.append(f'- [{r["category"]}] "{r["key"]}": "{r["value"][:120]}" (relevance: {r["score"]:.2f})')
            if len(lines) > 1:
                similarity_context = "\n".join(lines)
    except Exception:
        pass
    if not similarity_context:
        similarity_context = "(No similar existing entries found.)"

    # Build prompt
    prompt = _CONSOLIDATE_PROMPT_TEMPLATE_STANDALONE
    prompt = prompt.replace("<<CURRENT_MEMORY>>", current_memory)
    prompt = prompt.replace("<<SIMILARITY_CONTEXT>>", similarity_context)
    prompt = prompt.replace("<<CONVERSATION_SUMMARY>>", conv_text)

    # Fallback chain
    result: dict[str, Any] | None = None
    attempts: list[str] = []

    result = await _try_consolidation_call(model, prompt)
    if result:
        attempts.append(f"{model} ✓")
    else:
        attempts.append(f"{model} ✗")
        api_backend = _resolve_api_backend(model)
        if not api_backend and browser_profiles:
            for profile in browser_profiles[:3]:
                result = await _try_consolidation_call(model, prompt, browser_profile=profile)
                if result:
                    attempts.append(f"{model}@{profile} ✓")
                    break
                attempts.append(f"{model}@{profile} ✗")
        if not result and fallback_models:
            for fb_model in fallback_models[:3]:
                result = await _try_consolidation_call(fb_model, prompt)
                if result:
                    attempts.append(f"{fb_model} ✓")
                    break
                attempts.append(f"{fb_model} ✗")

    if not result:
        _store_consolidation_result(chat_id, {"status": "error", "detail": f"All consolidation attempts failed: {' → '.join(attempts)}"})
        return

    raw_answer = str(result.get("answer", ""))
    cleaned = raw_answer.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()
    try:
        new_entries = json.loads(cleaned)
    except json.JSONDecodeError:
        start_idx = cleaned.find("{")
        end_idx = cleaned.rfind("}")
        if start_idx != -1 and end_idx > start_idx:
            try:
                new_entries = json.loads(cleaned[start_idx : end_idx + 1])
            except json.JSONDecodeError:
                _store_consolidation_result(chat_id, {"status": "error", "detail": "Model returned invalid JSON", "raw": raw_answer[:500]})
                return
        else:
            _store_consolidation_result(chat_id, {"status": "error", "detail": "No JSON object found in response", "raw": raw_answer[:500]})
            return

    if not isinstance(new_entries, dict):
        _store_consolidation_result(chat_id, {"status": "error", "detail": "Expected dict with add/delete keys"})
        return

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
    if mem_path.exists():
        try:
            existing = json.loads(mem_path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except Exception:
            existing = {}

    # Dedup with conflict collection
    review_queue: list[dict[str, Any]] = []
    try:
        searcher = get_searcher()
        adds, review_queue = _dedup_and_resolve_adds(adds, existing, searcher)
    except Exception:
        review_queue = []

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
                existing_list.append(_build_stored_entry(entry, cat))
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

    skill_created = False
    skill_data = new_entries.get("create_skill")
    if isinstance(skill_data, dict) and skill_data.get("name"):
        skill_created = _save_user_skill(skill_data)

    total_added = added_count + prot_added + eph_added

    # Personality assessment — fire-and-forget within the thread
    asyncio.create_task(_run_personality_assessment(conv_text, model))

    # Background dedup review — fire-and-forget, never blocks response
    if review_queue:
        asyncio.create_task(_run_dedup_review(review_queue, mem_path, model))

    final: dict[str, Any] = {"status": "ok", "added": total_added, "deleted": deleted_count, "dedup_review_count": len(review_queue)}
    if skill_created:
        final["skill_created"] = skill_data["name"]
    _store_consolidation_result(chat_id, final)


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


@router.delete("/api/settings/memory")
async def delete_memory_entry(payload: dict[str, Any]) -> dict[str, str]:
    """Delete a single memory entry by category and key."""
    category = payload.get("category", "")
    key = payload.get("key", "")
    if not category or not key:
        raise HTTPException(status_code=400, detail="Missing 'category' or 'key'")
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
    cfg: dict[str, Any] = {"enabled": True, "top_skill": 5, "top_memory": 4, "top_total": 9}
    if _MEMORY_SEARCH_SETTINGS.exists():
        try:
            cfg = json.loads(_MEMORY_SEARCH_SETTINGS.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "enabled": cfg.get("enabled", True),
        "top_skill": cfg.get("top_skill", 5),
        "top_memory": cfg.get("top_memory", 4),
        "top_total": cfg.get("top_total", 9),
        "max_prompt_chars": cfg.get("max_prompt_chars", _DEFAULT_MAX_PROMPT_CHARS),
        "model_thresholds": searcher.get_custom_thresholds(),
        "current_model": searcher.model_name,
        "current_threshold": searcher.threshold,
        "current_proc_threshold": searcher.get_proc_threshold(),
        "available_models": list_available_models(),
    }

@router.post("/api/settings/memory-search")
async def update_memory_search_settings(payload: dict[str, Any]) -> dict[str, Any]:
    model = payload.get("model")
    top_skill = payload.get("top_skill")
    top_memory = payload.get("top_memory")
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
        clean: dict[str, Any] = {}
        for k, v in model_thresholds.items():
            if v in (None, "", "auto"):
                continue
            if isinstance(v, dict):
                # New format: {"proc": X, "std": Y}
                entry: dict[str, float] = {}
                if "proc" in v and v["proc"] not in (None, "", "auto"):
                    try:
                        entry["proc"] = float(v["proc"])
                    except (TypeError, ValueError):
                        pass
                if "std" in v and v["std"] not in (None, "", "auto"):
                    try:
                        entry["std"] = float(v["std"])
                    except (TypeError, ValueError):
                        pass
                if entry:
                    clean[str(k)] = entry
            else:
                # Legacy: single float
                try:
                    clean[str(k)] = float(v)
                except (TypeError, ValueError):
                    continue
        cfg["model_thresholds"] = clean
        get_searcher().set_thresholds(clean)
    if top_skill is not None:
        cfg["top_skill"] = int(top_skill)
    if top_memory is not None:
        cfg["top_memory"] = int(top_memory)
    if top_total is not None:
        cfg["top_total"] = int(top_total)
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
) -> tuple[dict[str, list[dict[str, str]]], list[dict[str, Any]]]:
    """Post-consolidation dedup with conflict collection for background LLM review.
    
    For each new entry in semantic/episodic/procedural:
    - Embed it and search against existing memory
    - If best match similarity >= 0.70: collect into review queue (LLM decides later)
    - Otherwise: add as new
    
    Also performs intra-batch dedup: cross-checks kept entries against each other
    using token overlap to catch duplicates born in the same consolidation pass.
    
    Returns (filtered_adds, review_queue).
    """
    REVIEW_THRESHOLD = 0.70
    INTRA_BATCH_OVERLAP = 0.60
    
    review_queue: list[dict[str, Any]] = []
    filtered: dict[str, list[dict[str, str]]] = {}
    
    for cat in ("semantic", "episodic", "procedural"):
        new_list = adds.get(cat, [])
        if not isinstance(new_list, list) or not new_list:
            filtered[cat] = []
            continue
        
        kept: list[dict[str, str]] = []
        for entry in new_list:
            if not isinstance(entry, dict) or not entry.get("key"):
                continue
            
            entry_text = f"{entry['key']}: {entry.get('value', '')}"
            
            # Search against ALL existing memory (not just same category)
            try:
                results = searcher.search(entry_text, top_k=3)
            except Exception:
                results = []
            
            if results:
                best = results[0]
                best_score = best.get("score", 0.0)
                
                if best_score >= REVIEW_THRESHOLD:
                    # High similarity — queue for background LLM review
                    review_queue.append({
                        "category": cat,
                        "new_entry": _build_stored_entry(entry, cat),
                        "existing_entry": {"key": best.get("key", ""), "value": best.get("value", "")},
                        "score": best_score,
                    })
                    continue
            
            kept.append(_build_stored_entry(entry, cat))
        
        # Intra-batch dedup: cross-check kept entries against each other
        if len(kept) > 1:
            deduped_kept: list[dict[str, str]] = [kept[0]]
            for candidate in kept[1:]:
                cand_text = f"{candidate['key']} {candidate.get('value', '')}".lower()
                cand_tokens = set(cand_text.split())
                is_dup = False
                for accepted in deduped_kept:
                    acc_text = f"{accepted['key']} {accepted.get('value', '')}".lower()
                    acc_tokens = set(acc_text.split())
                    if not cand_tokens or not acc_tokens:
                        continue
                    overlap = len(cand_tokens & acc_tokens) / max(len(cand_tokens), len(acc_tokens))
                    if overlap >= INTRA_BATCH_OVERLAP:
                        review_queue.append({
                            "category": cat,
                            "new_entry": candidate,
                            "existing_entry": accepted,
                            "score": overlap,
                        })
                        is_dup = True
                        break
                if not is_dup:
                    deduped_kept.append(candidate)
            kept = deduped_kept
        
        filtered[cat] = kept
    
    # Pass through protected and ephemeral unchanged
    filtered["protected"] = adds.get("protected", [])
    filtered["ephemeral"] = adds.get("ephemeral", [])
    
    return filtered, review_queue


async def _run_dedup_review(
    review_queue: list[dict[str, Any]],
    mem_path: Path,
    model: str,
) -> None:
    """Background LLM review of high-similarity memory conflicts.
    
    For each conflict pair, asks the LLM to decide: merge, replace, or keep_both.
    Applies decisions directly to the memory file. Non-blocking — runs as asyncio task.
    """
    if not review_queue:
        return
    
    try:
        data = json.loads(mem_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
    except Exception:
        logger.warning("dedup_review: failed to read memory file, skipping review")
        return
    
    applied = 0
    skipped_llm = 0
    
    for conflict in review_queue:
        cat = conflict["category"]
        new_entry = conflict["new_entry"]
        existing_entry = conflict["existing_entry"]
        score = conflict["score"]
        
        # Build review prompt
        prompt = _DEDUP_REVIEW_PROMPT_TEMPLATE
        prompt = prompt.replace("<<EXISTING_KEY>>", existing_entry.get("key", ""))
        prompt = prompt.replace("<<EXISTING_VALUE>>", existing_entry.get("value", ""))
        prompt = prompt.replace("<<NEW_KEY>>", new_entry.get("key", ""))
        prompt = prompt.replace("<<NEW_VALUE>>", new_entry.get("value", ""))
        prompt = prompt.replace("<<SCORE>>", f"{score:.2f}")
        
        try:
            result = await _try_consolidation_call(model, prompt)
        except Exception:
            logger.warning(f"dedup_review: LLM call failed for '{new_entry.get('key')}', leaving un-added")
            skipped_llm += 1
            continue
        
        if not result:
            skipped_llm += 1
            continue
        
        raw = result.get("answer", "").strip()
        # Strip markdown fences
        if raw.startswith("```"):
            lines = raw.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            raw = "\n".join(lines).strip()
        
        # Parse JSON
        try:
            decision_data = json.loads(raw)
        except json.JSONDecodeError:
            start = raw.find("{")
            end = raw.rfind("}")
            if start != -1 and end > start:
                try:
                    decision_data = json.loads(raw[start:end + 1])
                except json.JSONDecodeError:
                    logger.warning(f"dedup_review: invalid JSON for '{new_entry.get('key')}'")
                    skipped_llm += 1
                    continue
            else:
                skipped_llm += 1
                continue
        
        action = str(decision_data.get("action", "")).lower().strip()
        entries = decision_data.get("entries", [])
        if not isinstance(entries, list):
            entries = []
        
        cat_list: list[dict[str, str]] = data.get(cat, [])
        if not isinstance(cat_list, list):
            cat_list = []
        
        if action == "merge":
            # Remove both originals, add merged entry
            existing_key = existing_entry.get("key", "")
            new_key = new_entry.get("key", "")
            cat_list = [e for e in cat_list if not (isinstance(e, dict) and e.get("key") in (existing_key, new_key))]
            if entries:
                merged = entries[0]
                cat_list.append(_build_stored_entry(merged, cat))
            data[cat] = cat_list
            logger.info(f"dedup_review: MERGE '{existing_key}' + '{new_key}'")
            applied += 1
        
        elif action == "replace":
            # Remove outdated entry, add survivor
            existing_key = existing_entry.get("key", "")
            new_key = new_entry.get("key", "")
            cat_list = [e for e in cat_list if not (isinstance(e, dict) and e.get("key") in (existing_key, new_key))]
            if entries:
                survivor = entries[0]
                cat_list.append(_build_stored_entry(survivor, cat))
            data[cat] = cat_list
            logger.info(f"dedup_review: REPLACE '{existing_key}' ← '{new_key}'")
            applied += 1
        
        elif action == "keep_both":
            # Ensure both entries exist
            existing_keys = {e.get("key", "") for e in cat_list if isinstance(e, dict)}
            for e in entries:
                ek = str(e.get("key", ""))
                if ek and ek not in existing_keys:
                    cat_list.append(_build_stored_entry(e, cat))
                    existing_keys.add(ek)
            data[cat] = cat_list
            logger.info(f"dedup_review: KEEP_BOTH '{new_entry.get('key')}' + '{existing_entry.get('key')}'")
            applied += 1
        
        else:
            logger.warning(f"dedup_review: unknown action '{action}' for '{new_entry.get('key')}', leaving un-added")
            skipped_llm += 1
    
    # Write updated memory back to disk
    if applied > 0:
        try:
            mem_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            get_searcher().reload_memory()
            logger.info(f"dedup_review: applied {applied} decisions, {skipped_llm} skipped")
        except Exception as exc:
            logger.warning(f"dedup_review: failed to write memory file: {exc}")


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


@router.post("/api/memory/consolidate")
async def consolidate_memory(payload: dict[str, Any]) -> dict[str, Any]:
    """Kick off consolidation in a background thread. Returns instantly."""
    chat_id = payload.get("chat_id")
    if not chat_id:
        raise HTTPException(status_code=400, detail="Missing 'chat_id'")

    # Quick validation only — heavy work deferred to thread
    messages = await asyncio.to_thread(get_messages, chat_id)
    if len(messages) < 2:
        return {"status": "skipped", "reason": "too few messages"}

    settings = _load_consolidation_settings()
    model = settings.get("model") or payload.get("model") or ""
    if not model:
        return {"status": "error", "detail": "No model specified"}

    # Resolve project-scoped memory path
    _mem_path = _MEMORY_PATH
    _proj_id: str | None = None
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

    # Build memory context (lightweight enough for main thread)
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
    conv_text = _format_conversation(messages)

    # Spawn background thread — zero blocking
    t = threading.Thread(
        target=_run_consolidation_in_thread,
        args=(chat_id, _mem_path, messages, model,
              settings.get("fallback_models", []),
              settings.get("browser_profiles", []),
              current_memory, conv_text, _proj_id),
        daemon=True,
    )
    t.start()

    return {"status": "processing", "chat_id": chat_id}


@router.get("/api/memory/consolidate/result/{chat_id}")
async def get_consolidation_result(chat_id: str) -> dict[str, Any]:
    """Poll endpoint for background consolidation results."""
    result = _get_consolidation_result(chat_id)
    if result is None:
        return {"status": "pending"}
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

    # Build similarity context for scraper endpoint
    similarity_context = ""
    try:
        searcher = get_searcher()
        conv_text_sim = _format_conversation(messages)
        sim_results = searcher.search(conv_text_sim[:3000], top_k=10)
        if sim_results:
            lines = [_SIMILARITY_CONTEXT_HEADER]
            for r in sim_results:
                if r.get("score", 0) >= 0.5:
                    lines.append(f'- [{r["category"]}] "{r["key"]}": "{r["value"][:120]}" (relevance: {r["score"]:.2f})')
            if len(lines) > 1:
                similarity_context = "\n".join(lines)
    except Exception:
        pass
    if not similarity_context:
        similarity_context = "(No similar existing entries found.)"

    prompt = _CONSOLIDATE_PROMPT_TEMPLATE_HISTORY.replace("<<CURRENT_MEMORY>>", current_memory)
    prompt = prompt.replace("<<SIMILARITY_CONTEXT>>", similarity_context)
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
    if _mem_path.exists():
        try:
            existing = json.loads(_mem_path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except Exception:
            existing = {}
    # Post-consolidation dedup with conflict collection
    review_queue: list[dict[str, Any]] = []
    try:
        searcher = get_searcher()
        adds, review_queue = _dedup_and_resolve_adds(adds, existing, searcher)
    except Exception:
        review_queue = []
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
                existing_list.append(_build_stored_entry(entry, cat))
                existing_keys.add(entry["key"])
                added_count += 1
        existing[cat] = existing_list
    prot_new = adds.get("protected", [])
    prot_added = 0
    if isinstance(prot_new, list) and prot_new:
        existing_prot: list[dict[str, str]] = []
        # Protected memory ALWAYS read from universal
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
        # Protected memory ALWAYS goes to universal, never project-scoped
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
    _mem_path.parent.mkdir(parents=True, exist_ok=True)
    _mem_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    if _mem_path != _MEMORY_PATH:
        # Project-scoped memory — reload project searcher
        try:
            from engine.memory_search import reload_project_searcher
            reload_project_searcher(_proj_id)
        except Exception:
            pass
    else:
        get_searcher().reload_memory()
    total_added = added_count + prot_added + eph_added

    # Fire personality assessment in background
    conv_text = _format_conversation(messages)
    asyncio.create_task(_run_personality_assessment(conv_text, model))

    # Background dedup review — fire-and-forget
    if review_queue:
        asyncio.create_task(_run_dedup_review(review_queue, _mem_path, model))

    return {"status": "ok", "added": total_added, "deleted": deleted_count, "dedup_review_count": len(review_queue)}