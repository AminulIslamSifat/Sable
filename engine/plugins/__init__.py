"""Startup plugins that populate or refresh Sable's model registry."""

from engine.plugins.opencode_free import sync as sync_opencode_free

__all__ = ["sync_opencode_free"]
