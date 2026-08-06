
"""MCPManager — spawns, connects to, and routes calls through MCP servers.

Each configured server runs as a subprocess communicating over stdio.
The manager keeps connections alive via AsyncExitStack and exposes
discovered tools into Sable's skill routing layer.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import Any

from engine.config import _ROOT

logger = logging.getLogger("sable.mcp")

MCP_CONFIG_PATH = _ROOT / "system" / "mcp_servers.json"


class MCPServerConnection:
    """Holds a single live MCP server connection."""

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        self.name = name
        self.config = config
        self.session: Any = None
        self.tools: list[dict[str, Any]] = []
        self._exit_stack: contextlib.AsyncExitStack | None = None
        self._connected = False
        self._error: str | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def error(self) -> str | None:
        return self._error

    async def connect(self) -> None:
        """Spawn the server subprocess and initialize the MCP session."""
        if self._connected:
            return

        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            self._exit_stack = contextlib.AsyncExitStack()

            server_params = StdioServerParameters(
                command=self.config["command"],
                args=self.config.get("args", []),
                env=self.config.get("env") or None,
            )

            read, write = await self._exit_stack.enter_async_context(
                stdio_client(server_params)
            )
            self.session = await self._exit_stack.enter_async_context(
                ClientSession(read, write)
            )
            await self.session.initialize()

            # Discover tools
            tools_result = await self.session.list_tools()
            self.tools = [
                {
                    "name": t.name,
                    "description": t.description or "",
                    "inputSchema": t.inputSchema if hasattr(t, "inputSchema") else {},
                }
                for t in tools_result.tools
            ]

            self._connected = True
            self._error = None
            logger.info("MCP server '%s' connected — %d tools discovered", self.name, len(self.tools))

        except Exception as exc:
            self._error = str(exc)
            self._connected = False
            logger.error("MCP server '%s' failed to connect: %s", self.name, exc)
            await self._cleanup()

    async def disconnect(self) -> None:
        """Gracefully shut down the server connection."""
        await self._cleanup()
        self._connected = False
        logger.info("MCP server '%s' disconnected", self.name)

    async def _cleanup(self) -> None:
        if self._exit_stack:
            with contextlib.suppress(Exception):
                await self._exit_stack.aclose()
            self._exit_stack = None
        self.session = None
        self.tools = []

    async def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Call a tool on this MCP server and return the result."""
        if not self._connected or not self.session:
            raise RuntimeError(f"MCP server '{self.name}' is not connected")

        result = await self.session.call_tool(tool_name, arguments or {})

        # Extract text content from MCP result
        if hasattr(result, "content"):
            texts = []
            for block in result.content:
                if hasattr(block, "text"):
                    texts.append(block.text)
            return "\n".join(texts) if texts else str(result)
        return str(result)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "connected": self._connected,
            "error": self._error,
            "tools": self.tools,
            "config": {k: v for k, v in self.config.items() if k != "env"},
        }


class MCPManager:
    """Manages all MCP server connections."""

    def __init__(self) -> None:
        self._connections: dict[str, MCPServerConnection] = {}
        self._lock = asyncio.Lock()

    def _load_config(self) -> dict[str, Any]:
        if MCP_CONFIG_PATH.exists():
            try:
                return json.loads(MCP_CONFIG_PATH.read_text())
            except (json.JSONDecodeError, OSError):
                logger.warning("MCP config file is corrupt, using empty config")
        return {"servers": {}}

    def _save_config(self, config: dict[str, Any]) -> None:
        MCP_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        MCP_CONFIG_PATH.write_text(json.dumps(config, indent=2))

    def get_server_configs(self) -> dict[str, Any]:
        return self._load_config().get("servers", {})

    def add_server(self, name: str, config: dict[str, Any]) -> None:
        cfg = self._load_config()
        cfg["servers"][name] = config
        self._save_config(cfg)

    def remove_server(self, name: str) -> bool:
        cfg = self._load_config()
        if name in cfg["servers"]:
            del cfg["servers"][name]
            self._save_config(cfg)
            return True
        return False

    def update_server(self, name: str, config: dict[str, Any]) -> bool:
        cfg = self._load_config()
        if name not in cfg["servers"]:
            return False
        cfg["servers"][name] = config
        self._save_config(cfg)
        return True

    async def connect_server(self, name: str) -> MCPServerConnection | None:
        """Connect to a configured MCP server by name."""
        async with self._lock:
            # Disconnect existing connection if any
            if name in self._connections:
                await self._isolate(self._connections[name].disconnect())

            configs = self.get_server_configs()
            if name not in configs:
                logger.error("MCP server '%s' not found in config", name)
                return None

            conn = MCPServerConnection(name, configs[name])
            await self._isolate(conn.connect())
            self._connections[name] = conn
            return conn

    async def disconnect_server(self, name: str) -> None:
        async with self._lock:
            if name in self._connections:
                await self._isolate(self._connections[name].disconnect())
                del self._connections[name]

    @staticmethod
    async def _isolate(coro) -> None:
        """Run a coroutine in a separate asyncio task.

        MCP's stdio_client creates anyio cancel scopes via task groups.
        Running inside FastAPI's ASGI middleware causes cancel-scope conflicts
        ('Attempted to exit a cancel scope that isn't the current task's').
        Spawning a fresh task isolates those scopes from the request context.
        """
        done = asyncio.Event()
        exc_holder: list[BaseException | None] = [None]

        async def _runner():
            try:
                await coro
            except BaseException as exc:
                exc_holder[0] = exc
            finally:
                done.set()

        asyncio.create_task(_runner())
        await done.wait()
        if exc_holder[0] is not None:
            raise exc_holder[0]

    async def connect_all_enabled(self) -> None:
        """Connect all servers marked as enabled in config."""
        configs = self.get_server_configs()
        for name, cfg in configs.items():
            if cfg.get("enabled", True):
                await self.connect_server(name)

    async def shutdown(self) -> None:
        """Disconnect all servers (called on app shutdown)."""
        async with self._lock:
            for conn in self._connections.values():
                await self._isolate(conn.disconnect())
            self._connections.clear()

    def get_connection(self, name: str) -> MCPServerConnection | None:
        return self._connections.get(name)

    def list_servers(self) -> list[dict[str, Any]]:
        """List all configured servers with their connection status."""
        configs = self.get_server_configs()
        result = []
        for name, cfg in configs.items():
            conn = self._connections.get(name)
            entry = {
                "name": name,
                "command": cfg.get("command", ""),
                "args": cfg.get("args", []),
                "env": cfg.get("env", {}),
                "enabled": cfg.get("enabled", True),
                "connected": conn.connected if conn else False,
                "error": conn.error if conn else None,
                "tools": conn.tools if conn else [],
            }
            result.append(entry)
        return result

    def get_all_tools(self) -> dict[str, list[dict[str, Any]]]:
        """Get all tools from all connected servers, keyed by server name."""
        result = {}
        for name, conn in self._connections.items():
            if conn.connected:
                result[name] = conn.tools
        return result

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Route a tool call to the appropriate MCP server."""
        conn = self._connections.get(server_name)
        if not conn or not conn.connected:
            raise RuntimeError(f"MCP server '{server_name}' is not connected")
        return await conn.call_tool(tool_name, arguments)

    async def call_tool_auto(self, tool_name: str, arguments: dict[str, Any] | None = None) -> tuple[str, Any]:
        """Find which server owns a tool and call it. Returns (server_name, result)."""
        for name, conn in self._connections.items():
            if conn.connected:
                for tool in conn.tools:
                    if tool["name"] == tool_name:
                        result = await conn.call_tool(tool_name, arguments)
                        return name, result
        raise RuntimeError(f"No connected MCP server provides tool '{tool_name}'")


    def get_prompt_section(self) -> str:
        """Generate a system prompt section listing connected MCP tools.

        Returns empty string if no servers are connected.
        """
        connected = {
            name: conn for name, conn in self._connections.items()
            if conn.connected and conn.tools
        }
        if not connected:
            return ""

        lines = [
            "## MCP Tools (External Servers)",
            "",
            "Connected MCP servers provide additional tools. Call them with:",
            '<mcp_call server="SERVER_NAME" tool="TOOL_NAME">{json_args}</mcp_call>',
            "Wrap in an action block like any other skill tag.",
            "",
        ]

        for server_name, conn in connected.items():
            lines.append(f"### Server: `{server_name}`")
            for tool in conn.tools:
                desc = tool.get("description", "").strip()
                if len(desc) > 120:
                    desc = desc[:117] + "..."
                schema = tool.get("inputSchema", {})
                props = schema.get("properties", {})
                required = schema.get("required", [])
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
            lines.append("")

        lines.append("> `*` = required param. Pass arguments as JSON in the tag body.")
        return chr(10).join(lines)


# Singleton
_manager: MCPManager | None = None


def get_mcp_manager() -> MCPManager:
    global _manager
    if _manager is None:
        _manager = MCPManager()
    return _manager
