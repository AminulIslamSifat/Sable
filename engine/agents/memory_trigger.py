
"""Agent memory trigger — threshold-based memory consolidation for subagents.

After agent completion, if tool usage exceeds configured thresholds,
automatically consolidates reusable knowledge from the agent's work session
into Brain/Memory.json.

Thresholds (any one triggers):
  - distinct tools used > 4
  - total tool calls > 12
  - error recoveries (failed tool calls) >= 3
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("sable.agents.memory_trigger")

_BRAIN_DIR = Path(__file__).resolve().parent.parent.parent / "Brain"
_MEMORY_PATH = _BRAIN_DIR / "Memory.json"
_PROTECTED_PATH = _BRAIN_DIR / "Protected.json"
_PROCEDURAL_PATH = _BRAIN_DIR / "Procedural.json"
_SETTINGS_PATH = Path(__file__).resolve().parent.parent.parent / "system" / "consolidation_settings.json"

# Thresholds — any single condition triggers consolidation
THRESHOLD_DISTINCT_TOOLS = 4
THRESHOLD_TOTAL_CALLS = 12
THRESHOLD_ERROR_RECOVERIES = 3

# Max conversation chars sent to consolidation LLM
_MAX_AGENT_CONV_CHARS = 30_000

# Similarity threshold for triggering merge resolution (pure cosine on normalized vectors)
_MERGE_SIMILARITY_THRESHOLD = 0.7

# Consolidation audit log
_CONSOLIDATION_LOG_PATH = Path(__file__).resolve().parent.parent.parent / "system" / "consolidation.txt"


def _log_consolidation(section: str, content: str) -> None:
    """Append a timestamped section to system/consolidation.txt for debugging."""
    from datetime import datetime
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"\n{'='*80}\n[{ts}] {section}\n{'='*80}\n{content}\n"
        _CONSOLIDATION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_CONSOLIDATION_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(entry)
    except Exception as exc:
        logger.warning("[memory_trigger] Failed to write consolidation log: %s", exc)


_AGENT_CONSOLIDATION_PROMPT = (
    "[SYSTEM: Agent memory consolidation. Extract reusable knowledge from this agent's work session.]\n\n"
    "CURRENT MEMORY STORE:\n<<CURRENT_MEMORY>>\n\n"
    "AGENT WORK SESSION:\n"
    "Role: <<AGENT_ROLE>>\n"
    "Task: <<AGENT_TASK>>\n"
    "Tools used: <<TOOLS_USED>>\n"
    "Total tool calls: <<TOOL_CALLS>>\n"
    "Error recoveries: <<ERROR_RECOVERIES>>\n\n"
    "CONVERSATION LOG:\n<<CONVERSATION>>\n\n"
    "TASK: Extract durable, reusable knowledge from this agent's work session.\n"
    "Focus on:\n"
    "- Procedures and workflows that worked (procedural category)\n"
    "- Error patterns and their fixes (semantic or episodic)\n"
    "- Architecture discoveries, file paths, tool behaviors (semantic)\n"
    "- Skip: trivial outputs, greetings, one-off details\n\n"
    "You are a strict filter. Most agent sessions produce 0-2 memories.\n\n"
    "CATEGORIES:\n"
    "- semantic: Durable facts — architecture, configs, paths, tool behaviors\n"
    "- episodic: Event-specific debugging sessions with reusable context\n"
    "- procedural: Workflows, patterns, how-to sequences\n\n"
    'OUTPUT: Raw JSON only. No markdown fences.\n'
    'Format:\n'
    '{\n'
    '  "add": {\n'
    '    "semantic": [{"key": "short_label", "value": "dense specific fact"}],\n'
    '    "episodic": [{"key": "short_label", "value": "context-rich event record"}],\n'
    '    "procedural": [{"key": "short_label", "value": "workflow or pattern description"}]\n'
    '  },\n'
    '  "delete": ["exact_existing_key_to_remove"]\n'
    '}\n'
    'If nothing qualifies: {"add":{"semantic":[],"episodic":[],"procedural":[]},"delete":[]}\n'
)


def should_trigger(agent) -> bool:
    """Check if agent's tool usage exceeds any memory trigger threshold."""
    distinct = len(agent.skills_used)
    return (
        distinct > THRESHOLD_DISTINCT_TOOLS
        or agent.tool_calls_total > THRESHOLD_TOTAL_CALLS
        or agent.error_recoveries >= THRESHOLD_ERROR_RECOVERIES
    )


def _format_agent_conversation(agent, max_chars: int = _MAX_AGENT_CONV_CHARS) -> str:
    """Format agent messages into a readable conversation for consolidation."""
    parts: list[str] = []
    total = 0
    for msg in agent.messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if not content or role == "system":
            continue
        # Skip the initial system prompt message
        if role == "user" and msg is agent.messages[1] if len(agent.messages) > 1 else False:
            pass  # Include the task message
        label = {"user": "User", "assistant": "Assistant", "tool": "Tool"}.get(role, role.capitalize())
        line = f"[{label}]: {content[:3000]}"  # Cap individual messages
        if total + len(line) > max_chars:
            parts.append(f"...[{len(agent.messages) - len(parts)} more messages truncated]")
            break
        parts.append(line)
        total += len(line)
    return "\n\n".join(parts)


def _load_consolidation_settings() -> dict[str, Any]:
    """Load consolidation model settings."""
    defaults: dict[str, Any] = {"model": "", "fallback_models": [], "browser_profiles": []}
    if _SETTINGS_PATH.exists():
        try:
            stored = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                defaults.update(stored)
        except Exception:
            pass
    return defaults


def _load_current_memory() -> str:
    """Load current memory as compact JSON string for context."""
    if not _MEMORY_PATH.exists():
        return "{}"
    try:
        data = json.loads(_MEMORY_PATH.read_text(encoding="utf-8"))
        # Only include keys for dedup context (not full values to save space)
        compact = {}
        for cat in ("semantic", "episodic", "procedural"):
            entries = data.get(cat, [])
            if entries:
                compact[cat] = [{"key": e.get("key", "")} for e in entries if isinstance(e, dict)]
        return json.dumps(compact, indent=1)
    except Exception:
        return "{}"


async def _resolve_candidates_against_memory(
    parsed: dict[str, Any],
    model: str,
    fallback_models: list[str],
) -> dict[str, Any]:
    """Multi-step merge resolution: compare candidates against existing memory via
    vector similarity, then batch-call LLM to decide accept/skip/merge/replace.

    Returns a modified parsed dict with resolved additions and deletions.
    """
    from instruction.mem_cmd import _MERGE_RESOLUTION_PROMPT

    # Collect all candidate entries from the consolidation result
    candidates: list[dict[str, Any]] = []
    adds = parsed.get("add", {})
    if isinstance(adds, dict):
        for cat in ("semantic", "episodic", "procedural", "protected", "ephemeral"):
            for entry in adds.get(cat, []):
                if isinstance(entry, dict) and entry.get("key") and entry.get("value"):
                    cand: dict[str, Any] = {
                        "key": entry["key"],
                        "value": entry["value"],
                        "category": cat,
                    }
                    if cat == "procedural":
                        if entry.get("trigger"):
                            cand["trigger"] = str(entry["trigger"])
                        if entry.get("keywords") and isinstance(entry["keywords"], list):
                            cand["keywords"] = [str(kw) for kw in entry["keywords"]]
                    candidates.append(cand)

    if not candidates:
        _log_consolidation("MERGE RESOLUTION — SKIP", "No candidate entries to resolve.")
        return parsed  # Nothing to resolve

    _log_consolidation(
        "MERGE RESOLUTION — CANDIDATES",
        json.dumps(candidates, indent=2, ensure_ascii=False),
    )

    # Search for similar existing memories using vector similarity
    try:
        from engine.memory_search import get_searcher
        searcher = get_searcher()
        searcher._ensure_loaded()
    except Exception as exc:
        logger.warning("[memory_trigger] Could not load searcher for merge resolution: %s", exc)
        return parsed  # Fall back to original behavior

    # Build candidate texts and embed them
    candidate_texts = [f"{c['key']}: {c['value']}" for c in candidates]
    try:
        candidate_vecs = searcher._embed_texts(candidate_texts, is_query=True)
    except Exception as exc:
        logger.warning("[memory_trigger] Failed to embed candidates: %s", exc)
        return parsed

    # Compute cosine similarity against all existing entries (vectors are already normalized)
    if searcher._normed_vectors is None or len(searcher._normed_vectors) == 0:
        return parsed  # No existing memory to compare against

    sim_matrix = candidate_vecs @ searcher._normed_vectors.T  # (n_candidates, n_existing)

    # For each candidate, find existing entries above threshold
    pairs_text_parts: list[str] = []
    has_conflicts = False

    for i, cand in enumerate(candidates):
        sims = sim_matrix[i]
        matches: list[tuple[int, float]] = []
        for j in range(len(sims)):
            if sims[j] >= _MERGE_SIMILARITY_THRESHOLD:
                matches.append((j, float(sims[j])))
        matches.sort(key=lambda x: -x[1])
        matches = matches[:5]  # Top 5 matches per candidate

        if matches:
            has_conflicts = True
            pair_block = f"CANDIDATE [{cand['category']}]:\n  key: {cand['key']}\n  value: {cand['value']}\n"
            if cand.get("trigger"):
                pair_block += f"  trigger: {cand['trigger']}\n"
            if cand.get("keywords"):
                pair_block += f"  keywords: {cand['keywords']}\n"
            pair_block += "\nSIMILAR EXISTING:\n"
            for idx, score in matches:
                meta = searcher._entry_meta[idx]
                pair_block += f"  [{meta['category']}] key={meta['key']} | score={score:.3f}\n    value: {meta['value']}\n"
            pairs_text_parts.append(pair_block)

    if not has_conflicts:
        _log_consolidation("MERGE RESOLUTION — NO CONFLICTS", f"No candidates above {_MERGE_SIMILARITY_THRESHOLD} similarity threshold. Skipping merge resolution.")
        logger.info("[memory_trigger] No candidates above %.2f similarity — skipping merge resolution", _MERGE_SIMILARITY_THRESHOLD)
        return parsed

    # Build the batch prompt
    pairs_text = "\n---\n".join(pairs_text_parts)
    prompt = _MERGE_RESOLUTION_PROMPT.replace("<<CANDIDATE_PAIRS>>", pairs_text)

    _log_consolidation("MERGE RESOLUTION — PAIRS SENT TO LLM", pairs_text)
    _log_consolidation("MERGE RESOLUTION — PROMPT", prompt)

    # Call LLM for resolution
    result = await _try_consolidation_call(model, prompt)
    if not result:
        for fb_model in fallback_models[:2]:
            result = await _try_consolidation_call(fb_model, prompt)
            if result:
                break

    if not result:
        _log_consolidation("MERGE RESOLUTION — LLM FAILED", f"All models failed (primary={model}, fallbacks={fallback_models[:2]})")
        logger.warning("[memory_trigger] Merge resolution LLM call failed — using original consolidation")
        return parsed

    raw_answer = result.get("answer", "")
    _log_consolidation("MERGE RESOLUTION — LLM RAW OUTPUT", raw_answer)

    resolved = _parse_consolidation_response(raw_answer)
    if not resolved or "decisions" not in resolved:
        _log_consolidation("MERGE RESOLUTION — PARSE FAILED", f"Could not parse JSON from LLM output. Raw: {raw_answer[:500]}")
        logger.warning("[memory_trigger] Invalid merge resolution JSON — using original consolidation")
        return parsed

    decisions = resolved["decisions"]
    if not isinstance(decisions, list):
        return parsed

    # Apply decisions to transform the parsed consolidation result
    # Build lookup: (category, key) -> decision
    decision_map: dict[tuple[str, str], dict[str, Any]] = {}
    for d in decisions:
        if isinstance(d, dict) and d.get("candidate_key") and d.get("action"):
            decision_map[(d.get("category", ""), d["candidate_key"])] = d

    new_adds: dict[str, list] = {}
    extra_deletes: list[str] = list(parsed.get("delete", []))

    for cat in ("semantic", "episodic", "procedural", "protected", "ephemeral"):
        cat_entries = adds.get(cat, []) if isinstance(adds, dict) else []
        resolved_entries: list[dict[str, Any]] = []

        for entry in cat_entries:
            if not isinstance(entry, dict) or not entry.get("key"):
                continue
            decision = decision_map.get((cat, entry["key"]))
            if not decision:
                # No decision found — keep as-is (accept by default)
                resolved_entries.append(entry)
                continue

            action = decision.get("action", "accept")
            if action == "skip":
                continue  # Drop this candidate
            elif action == "accept":
                resolved_entries.append(entry)
            elif action == "merge":
                merged_value = decision.get("merged_value") or entry.get("value", "")
                existing_key = decision.get("existing_key")
                # Replace the existing entry with merged version
                if existing_key:
                    extra_deletes.append(existing_key)
                merged_entry: dict[str, Any] = {"key": entry["key"], "value": merged_value}
                # Preserve trigger/keywords for procedural merges
                if cat == "procedural":
                    mt = decision.get("merged_trigger")
                    mk = decision.get("merged_keywords")
                    if mt:
                        merged_entry["trigger"] = str(mt)
                    else:
                        merged_entry["trigger"] = entry.get("trigger", "")
                    if mk and isinstance(mk, list):
                        merged_entry["keywords"] = [str(kw) for kw in mk]
                    elif entry.get("keywords"):
                        merged_entry["keywords"] = entry["keywords"]
                resolved_entries.append(merged_entry)
            elif action == "replace":
                existing_key = decision.get("existing_key")
                if existing_key:
                    extra_deletes.append(existing_key)
                resolved_entries.append(entry)

        if resolved_entries:
            new_adds[cat] = resolved_entries

    # Deduplicate deletes
    seen_deletes: set[str] = set()
    unique_deletes: list[str] = []
    for k in extra_deletes:
        if k and k not in seen_deletes:
            seen_deletes.add(k)
            unique_deletes.append(k)

    resolved_parsed = {"add": new_adds, "delete": unique_deletes}

    _log_consolidation(
        "MERGE RESOLUTION — FINAL DECISIONS",
        json.dumps(decisions, indent=2, ensure_ascii=False),
    )
    _log_consolidation(
        "MERGE RESOLUTION — RESOLVED RESULT",
        json.dumps(resolved_parsed, indent=2, ensure_ascii=False),
    )

    logger.info(
        "[memory_trigger] Merge resolution: %d decisions applied, %d candidates remaining, %d extra deletes",
        len(decisions),
        sum(len(v) for v in new_adds.values()),
        len(unique_deletes) - len(parsed.get("delete", [])),
    )
    return resolved_parsed


def _apply_memory_changes(result: dict[str, Any]) -> dict[str, int]:
    """Apply consolidation results to Memory.json. Returns counts."""
    adds = result.get("add", {})
    deletes = result.get("delete", [])

    if not isinstance(adds, dict):
        adds = {}
    if not isinstance(deletes, list):
        deletes = []

    # Load existing memory (non-procedural only)
    existing: dict[str, list] = {}
    if _MEMORY_PATH.exists():
        try:
            existing = json.loads(_MEMORY_PATH.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except Exception:
            existing = {}
    # Strip any leftover procedural from Memory.json (lives in Procedural.json now)
    existing.pop("procedural", None)
    # Load procedural from separate file for dedup
    if _PROCEDURAL_PATH.exists():
        try:
            pdata = json.loads(_PROCEDURAL_PATH.read_text(encoding="utf-8"))
            if isinstance(pdata, dict):
                existing["procedural"] = pdata.get("procedural", [])
        except Exception:
            pass

    # Protected keys are never deleted
    protected_keys: set[str] = set()
    if _PROTECTED_PATH.exists():
        try:
            pdata = json.loads(_PROTECTED_PATH.read_text(encoding="utf-8"))
            for e in pdata.get("protected", []):
                if isinstance(e, dict) and e.get("key"):
                    protected_keys.add(e["key"])
        except Exception:
            pass
    for e in existing.get("protected", []):
        if isinstance(e, dict) and e.get("key"):
            protected_keys.add(e["key"])

    # Apply deletions
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

    # Apply additions (skip duplicates by key)
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
                existing_list.append({"key": entry["key"], "value": entry.get("value", "")})
                existing_keys.add(entry["key"])
                added_count += 1
        existing[cat] = existing_list

    # Procedural entries go to separate Procedural.json
    proc_new = adds.get("procedural", [])
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
                proc_existing.append(new_entry)
                proc_keys.add(entry["key"])
                added_count += 1
        _PROCEDURAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PROCEDURAL_PATH.write_text(
            json.dumps({"procedural": proc_existing}, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # Write back Memory.json (non-procedural only)
    if added_count > 0 or deleted_count > 0:
        _MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        _MEMORY_PATH.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
        # Reload searcher cache
        try:
            from engine.memory_search import get_searcher
            get_searcher().reload_memory()
        except Exception:
            pass

    return {"added": added_count, "deleted": deleted_count}


async def _try_consolidation_call(model: str, prompt: str) -> dict[str, Any] | None:
    """Try a single consolidation LLM call. Returns result dict or None."""
    try:
        from server.utils import retry_async, _is_deepseek_api_model, _resolve_api_backend
        from connectors import get_connector
        from connectors.deepseek.client import get_client as get_deepseek_client
        from engine.config import get_model_config

        _CONSOLIDATION_BACKENDS = frozenset({"groq", "gemini", "mistral"})
        api_backend = _resolve_api_backend(model)

        if api_backend:
            if api_backend in _CONSOLIDATION_BACKENDS:
                connector = get_connector(api_backend)
                result = await retry_async(
                    lambda: connector.chat(
                        message=prompt,
                        model=model,
                        inject_instructions=False,
                        chat_id=None,
                    ),
                    label=f"agent_memory_consolidate_{api_backend}",
                )
            elif _is_deepseek_api_model(model):
                ds_cfg = get_model_config(model)
                ds_api_type = ds_cfg.get("api_model_type")
                result = await retry_async(
                    lambda: get_deepseek_client().chat(
                        message=prompt,
                        model=ds_api_type,
                        thinking_mode="fast",
                        chat_id=None,
                        inject_instructions=False,
                    ),
                    label="agent_memory_consolidate_ds",
                )
            else:
                from server.api.dependencies import service
                result = await retry_async(
                    lambda: service.chat(
                        message=prompt,
                        chat_id=None,
                        parent_id=None,
                        model=model,
                    ),
                    label="agent_memory_consolidate_api",
                )
        else:
            # Qwen / default service
            from server.api.dependencies import service
            result = await retry_async(
                lambda: service.chat(
                    message=prompt,
                    chat_id=None,
                    parent_id=None,
                    model=model,
                ),
                label="agent_memory_consolidate_default",
            )

        answer = result.get("answer", "").strip()
        if answer:
            return result
        return None
    except Exception as exc:
        logger.debug("Consolidation call failed for %s: %s", model, exc)
        return None


def _parse_consolidation_response(raw: str) -> dict[str, Any] | None:
    """Parse JSON from consolidation LLM response."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except json.JSONDecodeError:
                return None
    return None


async def trigger_agent_memory(agent) -> dict[str, Any] | None:
    """Run memory consolidation for a completed agent if thresholds are met.

    Returns summary dict or None if not triggered / failed.
    """
    if not should_trigger(agent):
        return None

    logger.info(
        "[memory_trigger] Agent %s (%s) triggered: %d distinct tools, %d calls, %d errors",
        agent.id, agent.role, len(agent.skills_used),
        agent.tool_calls_total, agent.error_recoveries,
    )

    # Load settings
    settings = _load_consolidation_settings()
    model = settings.get("model", "")
    if not model:
        logger.warning("[memory_trigger] No consolidation model configured, skipping")
        return None

    fallback_models = settings.get("fallback_models", [])

    # Build prompt
    conversation = _format_agent_conversation(agent)
    current_memory = _load_current_memory()

    prompt = _AGENT_CONSOLIDATION_PROMPT
    prompt = prompt.replace("<<CURRENT_MEMORY>>", current_memory)
    prompt = prompt.replace("<<AGENT_ROLE>>", agent.role)
    prompt = prompt.replace("<<AGENT_TASK>>", agent.task)
    prompt = prompt.replace("<<TOOLS_USED>>", ", ".join(agent.skills_used) or "none")
    prompt = prompt.replace("<<TOOL_CALLS>>", str(agent.tool_calls_total))
    prompt = prompt.replace("<<ERROR_RECOVERIES>>", str(agent.error_recoveries))
    prompt = prompt.replace("<<CONVERSATION>>", conversation)

    _log_consolidation(f"STEP 1 — INITIAL CONSOLIDATION PROMPT (agent={agent.id})", prompt)

    # Try primary model, then fallbacks
    result = await _try_consolidation_call(model, prompt)
    if not result:
        for fb_model in fallback_models[:2]:
            result = await _try_consolidation_call(fb_model, prompt)
            if result:
                break

    if not result:
        _log_consolidation(f"STEP 1 — LLM FAILED (agent={agent.id})", f"All models failed (primary={model}, fallbacks={fallback_models[:2]})")
        logger.warning("[memory_trigger] All consolidation attempts failed for agent %s", agent.id)
        return {"status": "failed", "agent_id": agent.id}

    # Parse response
    raw_answer = result.get("answer", "")
    _log_consolidation(f"STEP 1 — LLM RAW OUTPUT (agent={agent.id})", raw_answer)

    parsed = _parse_consolidation_response(raw_answer)
    if not parsed:
        _log_consolidation(f"STEP 1 — PARSE FAILED (agent={agent.id})", f"Could not parse JSON. Raw: {raw_answer[:500]}")
        logger.warning("[memory_trigger] Invalid JSON from consolidation for agent %s", agent.id)
        return {"status": "parse_error", "agent_id": agent.id}

    _log_consolidation(f"STEP 1 — PARSED RESULT (agent={agent.id})", json.dumps(parsed, indent=2, ensure_ascii=False))

    # Multi-step merge resolution: compare candidates against existing memory
    parsed = await _resolve_candidates_against_memory(parsed, model, fallback_models)

    # Apply changes
    counts = _apply_memory_changes(parsed)
    logger.info(
        "[memory_trigger] Agent %s: +%d memories, -%d deleted",
        agent.id, counts["added"], counts["deleted"],
    )

    # Emit event to agent stream for panel visibility
    agent.push_stream_event({
        "type": "memory_trigger",
        "added": counts["added"],
        "deleted": counts["deleted"],
        "reason": f"tools={len(agent.skills_used)}, calls={agent.tool_calls_total}, errors={agent.error_recoveries}",
    })

    return {
        "status": "ok",
        "agent_id": agent.id,
        "added": counts["added"],
        "deleted": counts["deleted"],
    }
