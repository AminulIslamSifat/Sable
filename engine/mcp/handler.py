
"""handle_mcp_call — routes <mcp_call> agentic tags through the MCPManager.

Handlers execute in a worker thread (chat stream via run_in_executor, agents
via asyncio.to_thread), but live MCP sessions are bound to the main event
loop. We bridge the two by scheduling the coroutine onto the loop cached on
the agent runtime (get_runtime()._loop) with run_coroutine_threadsafe and
blocking the worker thread for the result.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Generator
from typing import Any

from engine.skills.handlers.common import _end_event, _output_event

_DEFAULT_MCP_TIMEOUT = 90
_MAX_MCP_TIMEOUT = 300


def handle_mcp_call(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()

    server = attrs.get("server", "").strip()
    tool = attrs.get("tool", "").strip()

    if not tool:
        yield _output_event(tag_id, "Missing required 'tool' attribute\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Missing 'tool' attribute")
        return

    # Parse JSON arguments from the tag body
    body = content.strip()
    if body:
        try:
            arguments = json.loads(body)
        except json.JSONDecodeError as exc:
            yield _output_event(tag_id, f"Invalid JSON arguments: {exc}\n", "stderr")
            yield _end_event(tag_id, name, False, started, error=f"Invalid JSON arguments: {exc}")
            return
    else:
        arguments = {}

    if not isinstance(arguments, dict):
        yield _output_event(tag_id, "Arguments must be a JSON object\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Arguments must be a JSON object")
        return

    try:
        timeout = int(attrs.get("timeout", _DEFAULT_MCP_TIMEOUT))
    except ValueError:
        timeout = _DEFAULT_MCP_TIMEOUT
    timeout = max(5, min(timeout, _MAX_MCP_TIMEOUT))

    args_preview = json.dumps(arguments, ensure_ascii=False)
    if len(args_preview) > 400:
        args_preview = args_preview[:400] + "…"
    yield _output_event(
        tag_id,
        f"$ mcp_call {server or '<auto>'}.{tool} {args_preview}\n",
        "command",
    )

    # Lazy imports to avoid circular dependencies at module load time
    from engine.mcp.manager import get_mcp_manager
    from engine.agents import get_runtime

    manager = get_mcp_manager()

    # Build the coroutine for the requested tool call
    if server:
        coro = manager.call_tool(server, tool, arguments)
    else:
        coro = manager.call_tool_auto(tool, arguments)

    # MCP sessions live on the main event loop; we are in a worker thread.
    loop = getattr(get_runtime(), "_loop", None)
    if loop is None or loop.is_closed():
        yield _output_event(tag_id, "No running event loop available for MCP call\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Event loop unavailable for MCP call")
        return

    try:
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        result = future.result(timeout=timeout)
    except TimeoutError:
        yield _output_event(tag_id, f"MCP call timed out after {timeout}s\n", "stderr")
        yield _end_event(tag_id, name, False, started, error=f"MCP call timed out after {timeout}s")
        return
    except Exception as exc:
        yield _output_event(tag_id, f"{type(exc).__name__}: {exc}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error=f"{type(exc).__name__}: {exc}")
        return

    # call_tool_auto returns (server_name, result); call_tool returns the result directly
    resolved_server = server
    if not server and isinstance(result, tuple) and len(result) == 2:
        resolved_server, result = result

    if isinstance(result, str):
        text = result
    else:
        try:
            text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
        except (TypeError, ValueError):
            text = str(result)

    if not text.strip():
        text = "(empty result)"

    yield _output_event(tag_id, text + "\n", "stdout")
    yield _end_event(
        tag_id,
        name,
        True,
        started,
        {"server": resolved_server, "tool": tool, "chars": len(text)},
    )
