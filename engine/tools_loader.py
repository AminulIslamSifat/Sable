
"""Tool discovery and schema loading with core/outer tier support.

Core tools: full schema always loaded into context.
Outer tools: only name + description loaded; full schema fetched on demand via load_tool.

Tier is declared per-tool-folder in a manifest.json file:
  {"tier": "core"} or {"tier": "outer"}
If no manifest.json exists, defaults to "core" for backward compatibility.

Scans the tools/ directory for tool.json manifests (flat arrays of
OpenAI-compatible function definitions) and provides schema data for
API endpoints and instruction injection.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"

# Tools that are always core regardless of manifest (safety net)
_ALWAYS_CORE = frozenset({
    "execute_command", "code_editor", "grep_search", "ask_user",
    "chat_title", "file_uploader",
})


@dataclass(slots=True)
class ToolMeta:
    """Parsed tool group (one folder = one tool group)."""
    key: str
    functions: list[dict] = field(default_factory=list)
    dir_path: Path = field(default_factory=Path)
    tier: str = "core"  # "core" or "outer"


def _read_tier(tool_dir: Path) -> str:
    """Read tier from manifest.json, default to 'core'."""
    manifest = tool_dir / "manifest.json"
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            tier = data.get("tier", "core").lower()
            if tier in ("core", "outer"):
                return tier
        except (json.JSONDecodeError, OSError):
            pass
    return "core"


def discover_tools(tools_dir: Path | None = None) -> list[ToolMeta]:
    """Scan tools_dir for tool.json files (flat arrays) and parse them."""
    if tools_dir is None:
        tools_dir = _TOOLS_DIR
    if not tools_dir.is_dir():
        logger.error("Tools directory does not exist: %s", tools_dir)
        return []

    tools: list[ToolMeta] = []
    for manifest_path in sorted(tools_dir.glob("*/tool.json")):
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to parse %s: %s", manifest_path, e)
            continue

        # tool.json is a flat array of function definitions
        if not isinstance(raw, list):
            logger.warning("tool.json is not an array: %s", manifest_path)
            continue

        key = manifest_path.parent.name
        tier = _read_tier(manifest_path.parent)
        # Safety net: some tools must always be core
        if key in _ALWAYS_CORE:
            tier = "core"

        meta = ToolMeta(
            key=key,
            functions=raw,
            dir_path=manifest_path.parent,
            tier=tier,
        )
        tools.append(meta)

    logger.info("Discovered %d tool groups (%d functions)", len(tools), sum(len(t.functions) for t in tools))
    return tools


def list_tools() -> list[dict]:
    """Return tool summaries for the /api/tools endpoint."""
    return [
        {"key": t.key, "name": t.key.replace("_", " ").title(), "functions": len(t.functions)}
        for t in discover_tools()
    ]


def browse_tools() -> list[dict]:
    """Return detailed tool info for the /api/tools/browse endpoint."""
    result = []
    for t in discover_tools():
        result.append({
            "key": t.key,
            "name": t.key.replace("_", " ").title(),
            "tools": t.functions,
            "path": str(t.dir_path),
        })
    return result


def get_all_tool_schemas(
    disabled: list[str] | None = None,
    allowed: list[str] | None = None,
    tier: str | None = None,
) -> list[dict]:
    """Return flat list of tool function schemas (OpenAI-compatible).

    Args:
        disabled: Tool keys to exclude.
        allowed: If set, only include these tool keys.
        tier: "core" = only core, "outer" = only outer, None = all.
    """
    disabled = disabled or []
    schemas = []
    for t in discover_tools():
        if t.key in disabled:
            continue
        if allowed and t.key not in allowed:
            continue
        if tier and t.tier != tier:
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
    stubs = []
    for t in discover_tools():
        if t.key in disabled or t.tier != "outer":
            continue
        for fn in t.functions:
            stubs.append({
                "type": "function",
                "function": {
                    "name": fn["name"],
                    "description": fn.get("description", "") + " [OUTER: use load_tool to activate]",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                    },
                },
            })
    return stubs


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
    result = []
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
    core_schemas = get_all_tool_schemas(disabled, tier="core")
    outer_stubs = get_outer_tool_stubs(disabled)

    # Inject load_tool as a core tool
    load_tool_schema = _build_load_tool_schema(disabled)

    all_schemas = core_schemas + outer_stubs
    if load_tool_schema:
        all_schemas.append(load_tool_schema)

    if not all_schemas:
        return ""

    lines = ["<tools>"]
    for s in all_schemas:
        lines.append(json.dumps(s, ensure_ascii=False))
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