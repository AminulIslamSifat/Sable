"""handle_mcp_list — structured discovery of connected MCP servers and tools.

Replaces the side-channel where load_tool("mcp_call") appended a prose dump of
connected servers. This is a real tool: structured, filterable, and
self-contained.
"""

from __future__ import annotations

import json
import time
from collections.abc import Generator
from typing import Any

from engine.skills.handlers.common import _end_event, _output_event


def handle_mcp_list(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()

    # Content may carry JSON args when the model nests params in the body
    args: dict[str, Any] = {}
    body = (content or "").strip()
    if body.startswith("{"):
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                args = parsed
        except json.JSONDecodeError:
            pass

    want_server = (attrs.get("server") or args.get("server") or "").strip()
    want_tool = (attrs.get("tool") or args.get("tool") or "").strip()

    from engine.mcp.manager import get_mcp_manager

    mgr = get_mcp_manager()
    servers = mgr.list_servers()

    payload: dict[str, Any] = {"servers": []}
    for entry in servers:
        srv_name = entry.get("name", "")
        if want_server and srv_name != want_server:
            continue

        tools_out = []
        for tool in entry.get("tools") or []:
            tname = tool.get("name", "")
            if want_tool and tname != want_tool:
                continue
            schema = tool.get("inputSchema") or {}
            props = schema.get("properties") or {}
            required = schema.get("required") or []
            params = {
                pname: {
                    "type": (pinfo or {}).get("type", "any"),
                    "required": pname in required,
                    "description": (pinfo or {}).get("description", ""),
                }
                for pname, pinfo in props.items()
            }
            tools_out.append({
                "name": tname,
                "description": tool.get("description", ""),
                "parameters": params,
            })

        payload["servers"].append({
            "name": srv_name,
            "connected": entry.get("connected", False),
            "error": entry.get("error"),
            "tool_count": len(tools_out),
            "tools": tools_out,
        })

    if not payload["servers"]:
        msg = "No MCP servers configured."
        if want_server:
            msg = f"No MCP server named '{want_server}'."
        yield _output_event(tag_id, msg + "\n")
        yield _end_event(tag_id, name, True, started, payload)
        return

    text = json.dumps(payload, indent=2, ensure_ascii=False)
    yield _output_event(tag_id, text + "\n")
    yield _end_event(tag_id, name, True, started, payload)
