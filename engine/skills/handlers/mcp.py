
"""Handler for MCP tool calls — routes <mcp_call> tags to the MCP manager."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Generator

logger = logging.getLogger(__name__)


def handle_mcp_call(
    tag_name: str,
    content: str,
    attrs: dict[str, str],
    raw_tag: str,
) -> Generator[dict[str, Any], None, None]:
    """Execute an MCP tool call.

    Expected attrs:
        server: MCP server name (e.g. "gmail")
        tool: Tool name to call (e.g. "send_email")

    Content is JSON arguments for the tool, or empty for no-arg tools.
    """
    from engine.mcp.manager import get_mcp_manager

    server_name = attrs.get("server", "").strip()
    tool_name = attrs.get("tool", "").strip()

    if not server_name:
        yield {"type": "skill_output", "content": "❌ Missing `server` attribute in mcp_call tag."}
        return
    if not tool_name:
        yield {"type": "skill_output", "content": "❌ Missing `tool` attribute in mcp_call tag."}
        return

    # Parse arguments from content
    arguments: dict[str, Any] = {}
    if content.strip():
        try:
            arguments = json.loads(content.strip())
        except json.JSONDecodeError:
            logger.error("mcp.py: %s", json)
            # Try to parse as key=value pairs
            arguments = {"input": content.strip()}

    manager = get_mcp_manager()

    try:
        # Run the async call in the event loop
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                result = loop.run_in_executor(
                    pool,
                    lambda: asyncio.run(manager.call_tool(server_name, tool_name, arguments)),
                )
                # Since we can't await in a sync generator, use a simpler approach
                result = asyncio.run(manager.call_tool(server_name, tool_name, arguments))
        else:
            result = asyncio.run(manager.call_tool(server_name, tool_name, arguments))

        yield {"type": "skill_output", "content": f"**MCP [{server_name}/{tool_name}]:**\n\n{result}"}

    except Exception as exc:
        logger.error("MCP call failed: %s/%s — %s", server_name, tool_name, exc)
        yield {"type": "skill_output", "content": f"❌ MCP call failed ({server_name}/{tool_name}): {exc}"}
