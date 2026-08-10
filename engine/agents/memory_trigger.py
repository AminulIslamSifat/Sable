
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
_SETTINGS_PATH = Path(__file__).resolve().parent.parent.parent / "system" / "consolidation_settings.json"

# Thresholds — any single condition triggers consolidation
THRESHOLD_DISTINCT_TOOLS = 4
THRESHOLD_TOTAL_CALLS = 12
THRESHOLD_ERROR_RECOVERIES = 3

# Max conversation chars sent to consolidation LLM
_MAX_AGENT_CONV_CHARS = 30_000

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


def _apply_memory_changes(result: dict[str, Any]) -> dict[str, int]:
    """Apply consolidation results to Memory.json. Returns counts."""
    adds = result.get("add", {})
    deletes = result.get("delete", [])

    if not isinstance(adds, dict):
        adds = {}
    if not isinstance(deletes, list):
        deletes = []

    # Load existing memory
    existing: dict[str, list] = {}
    if _MEMORY_PATH.exists():
        try:
            existing = json.loads(_MEMORY_PATH.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except Exception:
            existing = {}

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
        for cat in ("semantic", "episodic", "procedural", "ephemeral"):
            cat_list = existing.get(cat, [])
            before = len(cat_list)
            existing[cat] = [e for e in cat_list if not isinstance(e, dict) or e.get("key", "") not in delete_keys]
            deleted_count += before - len(existing[cat])

    # Apply additions (skip duplicates by key)
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

    # Write back
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

    # Try primary model, then fallbacks
    result = await _try_consolidation_call(model, prompt)
    if not result:
        for fb_model in fallback_models[:2]:
            result = await _try_consolidation_call(fb_model, prompt)
            if result:
                break

    if not result:
        logger.warning("[memory_trigger] All consolidation attempts failed for agent %s", agent.id)
        return {"status": "failed", "agent_id": agent.id}

    # Parse response
    raw_answer = result.get("answer", "")
    parsed = _parse_consolidation_response(raw_answer)
    if not parsed:
        logger.warning("[memory_trigger] Invalid JSON from consolidation for agent %s", agent.id)
        return {"status": "parse_error", "agent_id": agent.id}

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
