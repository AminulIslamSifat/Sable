"""load_tool handler — upgrades outer tool stubs to full schemas mid-conversation."""

from __future__ import annotations

import json
import time
from collections.abc import Generator
from typing import Any

from engine.skills.handlers.common import _end_event, _output_event


def handle_load_tool(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()

    tool_name = attrs.get("tool_name", "") or content.strip()
    if not tool_name:
        yield _output_event(tag_id, "Missing 'tool_name' parameter\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Missing tool_name")
        return

    from engine.tools_loader import load_single_tool, list_outer_tools

    schema = load_single_tool(tool_name)
    if schema is None:
        available = [t["name"] for t in list_outer_tools()]
        yield _output_event(
            tag_id,
            f"Tool '{tool_name}' not found. Available outer tools: {available}\n",
            "stderr",
        )
        yield _end_event(tag_id, name, False, started, error=f"Unknown tool: {tool_name}")
        return

    # Return the full schema so the model can see parameters on next turn.
    # MCP discovery lives in the dedicated mcp_list tool — no side-channel here.
    schema_json = json.dumps(schema, indent=2, ensure_ascii=False)
    hint = ""
    if tool_name == "mcp_call":
        hint = "\n> Use `mcp_list` to discover connected servers and their tool schemas."
    yield _output_event(tag_id, f"Loaded full schema for '{tool_name}':\n{schema_json}{hint}\n")
    yield _end_event(tag_id, name, True, started, {"loaded": tool_name, "schemas": schema})
