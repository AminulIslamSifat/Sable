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

    # For mcp_call, also include connected server tool listings
    extra_info = ""
    if tool_name == "mcp_call":
        try:
            from engine.mcp.manager import get_mcp_manager
            mgr = get_mcp_manager()
            connected = {
                srv: conn for srv, conn in mgr._connections.items()
                if conn.connected and conn.tools
            }
            if connected:
                lines = ["\nConnected MCP servers and their tools:"]
                for server_name, conn in connected.items():
                    lines.append(f"\n### Server: `{server_name}`")
                    for tool in conn.tools:
                        desc = tool.get("description", "").strip()
                        if len(desc) > 120:
                            desc = desc[:117] + "..."
                        schema_info = tool.get("inputSchema", {})
                        props = schema_info.get("properties", {})
                        required = schema_info.get("required", [])
                        if props:
                            params = []
                            for pname, pinfo in props.items():
                                req_mark = "*" if pname in required else ""
                                ptype = pinfo.get("type", "any")
                                params.append(f"{pname}{req_mark}: {ptype}")
                            lines.append(f"- **{tool['name']}**({', '.join(params)})")
                        else:
                            lines.append(f"- **{tool['name']}**()")
                        if desc:
                            lines.append(f"  {desc}")
                lines.append("\n> `*` = required param. Pass arguments as JSON in the tag body.")
                extra_info = "\n".join(lines)
        except Exception:
            pass

    # Return the full schema so the model can see parameters on next turn
    schema_json = json.dumps(schema, indent=2, ensure_ascii=False)
    yield _output_event(tag_id, f"Loaded full schema for '{tool_name}':\n{schema_json}{extra_info}\n")
    yield _end_event(tag_id, name, True, started, {"loaded": tool_name, "schemas": schema})
