"""OpenCode free-tier connector (direct HTTP, no binary)."""

from connectors.opencode.client import OpenCodeClient, get_client

__all__ = ["OpenCodeClient", "get_client"]
