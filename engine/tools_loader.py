
"""Tool discovery and schema loading.

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


@dataclass(slots=True)
class ToolMeta:
    """Parsed tool group (one folder = one tool group)."""
    key: str
    functions: list[dict] = field(default_factory=list)
    dir_path: Path = field(default_factory=Path)


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

        meta = ToolMeta(
            key=manifest_path.parent.name,
            functions=raw,
            dir_path=manifest_path.parent,
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


def get_all_tool_schemas(disabled: list[str] | None = None, allowed: list[str] | None = None) -> list[dict]:
    """Return flat list of all tool function schemas (OpenAI-compatible).

    Filters out disabled tools by key (folder name).
    If allowed is provided, only include tools whose key is in the allowed list.
    """
    disabled = disabled or []
    schemas = []
    for t in discover_tools():
        if t.key in disabled:
            continue
        if allowed and t.key not in allowed:
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


def get_tools_prompt_section(
    disabled: list[str] | None = None,
    provider: str | None = None,
) -> str:
    """Generate tools schema section ONLY.

    Returns just the <tools>...</tools> block with function schemas.
    Tool call FORMAT instructions are handled by instruction_builder.py
    which injects them with proper dual-placement weighting (primer before
    schemas + reinforcement at end of prompt). Do NOT add format instructions
    here — they would land after schemas and dilute the weighted placement.

    Args:
        disabled: Tool keys to exclude.
        provider: Kept for API compat but no longer affects output.
    """
    disabled = disabled or []
    schemas = get_all_tool_schemas(disabled)
    if not schemas:
        return ""

    lines = ["<tools>"]
    for s in schemas:
        lines.append(json.dumps(s, ensure_ascii=False))
    lines.append("</tools>")

    return "\n".join(lines)