"""Tool discovery and schema loading with core/outer tier support.

Core tools: full schema always loaded into context.
Outer tools: only name + description loaded; full schema fetched on demand via load_tool.

Tier is declared per-tool-folder in a manifest.json file:
  {"tier": "core"} or {"tier": "outer"}
A missing manifest defaults to "outer" — the CHEAP direction — so accidentally
adding a tool can never silently bloat every prompt with a full schema.
tools/validate.py flags missing manifests as an error so CI still catches them.

Discovery results are cached in-process so repeated schema lookups during a
single chat turn do not re-glob and re-parse the tools/ tree.  Call
``reload_tools()`` after adding/removing a tool at runtime.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"

# Tools that are always core regardless of manifest (safety net)
_ALWAYS_CORE = frozenset({
    "execute_command", "code_editor", "grep_search", "ask_user",
    "chat_title", "file_uploader",
})

# Valid tier values
_TIERS = frozenset({"core", "outer"})

# Default when a tool folder has no manifest.json. "outer" is the safe choice:
# a forgotten manifest costs a lazy load, not permanent context bloat.
_DEFAULT_TIER = "outer"

# Fields every function definition in tool.json must carry
_REQUIRED_FN_FIELDS = ("name", "description")


@dataclass(slots=True)
class ToolMeta:
    """Parsed tool group (one folder = one tool group)."""
    key: str
    functions: list[dict] = field(default_factory=list)
    dir_path: Path = field(default_factory=Path)
    tier: str = "core"  # "core" or "outer"
    # Layout modes that may see this tool. Empty = every mode. A tool that
    # declares ["study"] is offered only in study mode; tools with no modes key
    # stay available everywhere.
    modes: tuple[str, ...] = ()


# ─── Manifest parsing ────────────────────────────────────────────────────────

def _read_manifest(tool_dir: Path) -> tuple[str, tuple[str, ...]]:
    """Read ``(tier, modes)`` from manifest.json in a single pass.

    One read + one parse per tool — this used to be two separate helpers each
    re-reading and re-parsing the same file, doubling cold-cache I/O across
    the whole tools/ tree.

    Missing / invalid file -> ``(_DEFAULT_TIER, ())``. "outer" is the cheap,
    safe tier default so a forgotten manifest costs a lazy load rather than
    permanent context bloat; an empty modes tuple means "every mode", the
    permissive default so a shared tool needs no manifest change.
    """
    manifest = tool_dir / "manifest.json"
    if not manifest.exists():
        logger.warning("No manifest.json in %s — defaulting to '%s'", tool_dir, _DEFAULT_TIER)
        return _DEFAULT_TIER, ()
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Invalid manifest.json at %s: %s", manifest, exc)
        return _DEFAULT_TIER, ()
    if not isinstance(data, dict):
        logger.warning("manifest.json is not an object: %s", manifest)
        return _DEFAULT_TIER, ()

    tier = str(data.get("tier", _DEFAULT_TIER)).lower()
    if tier not in _TIERS:
        logger.warning("Unknown tier '%s' in %s, defaulting to '%s'", tier, manifest, _DEFAULT_TIER)
        tier = _DEFAULT_TIER

    raw_modes = data.get("modes")
    modes = (
        tuple(str(m).strip().lower() for m in raw_modes if str(m).strip())
        if isinstance(raw_modes, list)
        else ()
    )
    return tier, modes


def _validate_function(fn: Any, manifest_path: Path) -> dict | None:
    """Validate a single function definition. Returns a normalized dict or None."""
    if not isinstance(fn, dict):
        logger.warning("Non-object entry in %s: %r", manifest_path, fn)
        return None

    missing = [f for f in _REQUIRED_FN_FIELDS if not fn.get(f)]
    if missing:
        logger.warning("Entry in %s missing %s: %r", manifest_path, missing, fn.get("name"))
        return None

    name = fn["name"]
    if not isinstance(name, str) or not name.strip():
        logger.warning("Invalid tool name in %s: %r", manifest_path, name)
        return None

    params = fn.get("parameters")
    if params is None:
        params = {"type": "object", "properties": {}}
    elif not isinstance(params, dict):
        logger.warning("Invalid 'parameters' for %s in %s (expected object)", name, manifest_path)
        params = {"type": "object", "properties": {}}

    return {
        "name": name,
        "description": fn.get("description", ""),
        "parameters": params,
    }


# ─── Discovery (cached) ──────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _discover_cached(tools_dir_str: str) -> tuple[ToolMeta, ...]:
    """Cached discovery keyed by directory path. Returns an immutable tuple."""
    tools_dir = Path(tools_dir_str)
    if not tools_dir.is_dir():
        logger.error("Tools directory does not exist: %s", tools_dir)
        return ()

    tools: list[ToolMeta] = []
    for manifest_path in sorted(tools_dir.glob("*/tool.json")):
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to parse %s: %s", manifest_path, exc)
            continue

        if not isinstance(raw, list):
            logger.warning("tool.json is not an array: %s", manifest_path)
            continue

        functions = [f for f in (_validate_function(fn, manifest_path) for fn in raw) if f]
        if not functions:
            logger.warning("No valid functions in %s — skipping group", manifest_path)
            continue

        key = manifest_path.parent.name
        tier, modes = _read_manifest(manifest_path.parent)
        if key in _ALWAYS_CORE:
            tier = "core"

        tools.append(ToolMeta(
            key=key,
            functions=functions,
            dir_path=manifest_path.parent,
            tier=tier,
            modes=modes,
        ))

    total_fns = sum(len(t.functions) for t in tools)
    logger.info("Discovered %d tool groups (%d functions)", len(tools), total_fns)
    return tuple(tools)


def discover_tools(tools_dir: Path | None = None) -> list[ToolMeta]:
    """Return discovered tool groups (cached). Returns a fresh list per call."""
    key = str((tools_dir or _TOOLS_DIR).resolve())
    return list(_discover_cached(key))


def reload_tools() -> None:
    """Invalidate the discovery cache. Call after adding/removing a tool at runtime."""
    _discover_cached.cache_clear()
    # parser caches the derived name set separately — clear both or a newly
    # added tool stays invisible to the parser and the resilience repair regex.
    try:
        from engine.skills.parser import get_known_tags

        get_known_tags.cache_clear()
    except Exception:  # pragma: no cover - parser is optional here
        pass


# ─── Public schema helpers ───────────────────────────────────────────────────

def list_tools() -> list[dict]:
    """Return tool summaries for the /api/tools endpoint."""
    return [
        {"key": t.key, "name": t.key.replace("_", " ").title(), "functions": len(t.functions)}
        for t in discover_tools()
    ]


def browse_tools() -> list[dict]:
    """Return detailed tool info for the /api/tools/browse endpoint."""
    return [
        {
            "key": t.key,
            "name": t.key.replace("_", " ").title(),
            "tools": t.functions,
            "path": str(t.dir_path),
            "tier": t.tier,
        }
        for t in discover_tools()
    ]


def _mode_allows(t: ToolMeta, mode: str | None) -> bool:
    """Does a tool group belong in the requested layout mode?

    No declared modes -> shared, visible everywhere. Declared modes -> only
    when the current mode is listed. A mode-gated tool is therefore invisible
    to every other caller without any explicit block list.
    """
    if not t.modes:
        return True
    if mode is None:
        return False
    return mode in t.modes


def get_all_tool_schemas(
    disabled: list[str] | None = None,
    allowed: list[str] | None = None,
    tier: str | None = None,
    mode: str | None = None,
) -> list[dict]:
    """Return flat list of tool function schemas (OpenAI-compatible).

    Args:
        disabled: Tool keys to exclude.
        allowed: If set, only include these tool keys.
        tier: "core" = only core, "outer" = only outer, None = all.
        mode: Layout mode ("study", "agent", ...). Tools that declare a
              ``modes`` list are only included when the mode matches.
    """
    disabled = disabled or []
    schemas: list[dict] = []
    for t in discover_tools():
        if t.key in disabled:
            continue
        if allowed and t.key not in allowed:
            continue
        if tier and t.tier != tier:
            continue
        if not _mode_allows(t, mode):
            continue
        for fn in t.functions:
            schemas.append({
                "type": "function",
                "function": {
                    "name": fn["name"],
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
                },
            })
    return schemas


def get_outer_tool_stubs(disabled: list[str] | None = None) -> list[dict]:
    """Return lightweight stubs for outer tools (name + description only, no params).

    These get injected into the prompt so the model knows they exist
    and can request full schemas via load_tool.
    """
    disabled = disabled or []
    stubs: list[dict] = []
    for t in discover_tools():
        if t.key in disabled or t.tier != "outer":
            continue
        for fn in t.functions:
            stubs.append({
                "type": "function",
                "function": {
                    "name": fn["name"],
                    "description": fn.get("description", "") + " [OUTER: use load_tool to activate]",
                    "parameters": {"type": "object", "properties": {}},
                },
            })
    return stubs


# The tools study mode offers alongside its own four. Kept here (not in the
# route) so the schema gate and the study prompt can never disagree.
STUDY_SHARED_TOOLS: tuple[str, ...] = (
    "online_search",
    "image_generator",
    "code_editor",
    "file_uploader",
    "grep_search",
)


def get_study_tool_schemas(disabled: list[str] | None = None) -> list[dict]:
    """Full schemas for study mode: the four study tools + shared tools.

    Returns every study-gated tool (``modes`` contains "study") plus the
    allow-listed shared tools. Nothing else — no chat_title, no agent tooling.
    """
    disabled = disabled or []
    study_keys = {t.key for t in discover_tools() if "study" in t.modes}
    allowed = study_keys | set(STUDY_SHARED_TOOLS)
    return get_all_tool_schemas(disabled=disabled, allowed=list(allowed), mode="study")


def load_single_tool(tool_name: str) -> list[dict] | None:
    """Load full schema for a single tool by function name.

    Used by the load_tool handler to upgrade an outer stub to full schema.
    Returns list of function schemas or None if not found.
    """
    for t in discover_tools():
        for fn in t.functions:
            if fn["name"] == tool_name:
                return [{
                    "type": "function",
                    "function": {
                        "name": fn["name"],
                        "description": fn.get("description", ""),
                        "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
                    },
                }]
    return None


def list_outer_tools(disabled: list[str] | None = None) -> list[dict]:
    """Return summary of available outer tools for the load_tool description."""
    disabled = disabled or []
    result: list[dict] = []
    for t in discover_tools():
        if t.key in disabled or t.tier != "outer":
            continue
        for fn in t.functions:
            result.append({
                "name": fn["name"],
                "description": fn.get("description", ""),
            })
    return result


def get_tools_prompt_section(
    disabled: list[str] | None = None,
    provider: str | None = None,
) -> str:
    """Generate tools schema section with core/outer split.

    Core tools get full schemas. Outer tools get stubs + load_tool instruction.
    Returns just the <tools>...</tools> block.
    """
    disabled = disabled or []
    all_schemas = get_all_tool_schemas(disabled, tier="core")
    all_schemas.extend(get_outer_tool_stubs(disabled))

    load_tool_schema = _build_load_tool_schema(disabled)
    if load_tool_schema:
        all_schemas.append(load_tool_schema)

    if not all_schemas:
        return ""

    lines = ["<tools>"]
    lines.extend(json.dumps(s, ensure_ascii=False) for s in all_schemas)
    lines.append("</tools>")
    return "\n".join(lines)


def _build_load_tool_schema(disabled: list[str] | None = None) -> dict | None:
    """Build the load_tool function schema dynamically listing available outer tools."""
    outer = list_outer_tools(disabled)
    if not outer:
        return None

    tool_list = "\n".join(f"  - {t['name']}: {t['description']}" for t in outer)
    return {
        "type": "function",
        "function": {
            "name": "load_tool",
            "description": (
                "Load the full schema for an outer tool that is currently stubbed. "
                "Call this when you need to use a tool marked [OUTER]. "
                "After loading, the tool's full parameters become available.\n"
                f"Available outer tools:\n{tool_list}"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tool_name": {
                        "type": "string",
                        "description": "The name of the outer tool to load.",
                        "enum": [t["name"] for t in outer],
                    }
                },
                "required": ["tool_name"],
            },
        },
    }
