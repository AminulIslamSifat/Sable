
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
    """Generate tools reference section.

    Tools are declared inside <tools></tools> XML tags with one JSON object
    per line. The tool call format instructions vary by provider:
    - "deepseek": DSML invoke/parameter blocks.
    - "native"/None: Tag-wrapped Hermes format.
    - "none": Schemas only, no format instructions (native API function calling).
    """
    disabled = disabled or []
    schemas = get_all_tool_schemas(disabled)
    if not schemas:
        return ""

    lines = ["<tools>"]
    for s in schemas:
        lines.append(json.dumps(s, ensure_ascii=False))
    lines.append("</tools>")
    lines.append("")

    if provider == "deepseek":
        # DeepSeek V4 DSML format — native self-hosted tool calling
        _O = "<\uff5cDSML\uff5ctool_calls>"
        _C = "</\uff5cDSML\uff5ctool_calls>"
        _I = "<\uff5cDSML\uff5cinvoke"
        _P = "<\uff5cDSML\uff5cparameter"
        lines.append("You can invoke tools by writing a DSML block like the following:")
        lines.append(_O)
        lines.append(f'  {_I} name="$TOOL_NAME">')
        lines.append(f'    {_P} name="$PARAMETER_NAME" string="true|false">$VALUE</\uff5cDSML\uff5cparameter>')
        lines.append(f"  </\uff5cDSML\uff5cinvoke>")
        lines.append(_C)
        lines.append("")
        lines.append('String parameters: set string="true" and pass the raw text value.')
        lines.append('Non-string parameters (numbers, booleans, arrays, objects): set string="false" and pass JSON.')
        lines.append("")
        lines.append("Example:")
        lines.append(_O)
        lines.append(f'  {_I} name="execute_command">')
        lines.append(f'    {_P} name="command" string="true">ls -la</\uff5cDSML\uff5cparameter>')
        lines.append(f'    {_P} name="timeout" string="false">30</\uff5cDSML\uff5cparameter>')
        lines.append(f"  </\uff5cDSML\uff5cinvoke>")
        lines.append(_C)
        lines.append("")
        lines.append("You MUST strictly follow the tool name and parameter schemas defined above.")
        lines.append("Output ALL tool calls inside ONE DSML block. Multiple invokes are allowed inside one block.")
    elif provider == "none":
        # Native API function calling — schemas are sufficient
        lines.append("Use the function schemas above via native API function calling.")
    else:
        # Default: Hermes tag format
        TC_OPEN = "<" + "tool_call" + ">"
        TC_CLOSE = "</" + "tool_call" + ">"
        lines.append("CRITICAL: You MUST use exactly ONE opening tag and ONE closing tag per response.")
        lines.append("Single or multiple calls — always a JSON array inside one wrapper:")
        lines.append(TC_OPEN)
        lines.append('[{"name": "<function-name>", "arguments": <args-json-object>}]')
        lines.append(TC_CLOSE)
        lines.append("")
        lines.append(TC_OPEN)
        lines.append('[{"name": "tool_a", "arguments": {...}}, {"name": "tool_b", "arguments": {...}}]')
        lines.append(TC_CLOSE)
        lines.append("NEVER output multiple separate blocks. One wrapper only. Always.")

    return "\n".join(lines)
