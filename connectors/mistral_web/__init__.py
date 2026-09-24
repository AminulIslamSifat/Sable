"""Mistral (Web) connector — chat.mistral.ai / Vibe, browser-auth over plain SSE."""

from connectors.mistral_web.client import MistralWebClient, get_client

__all__ = ["MistralWebClient", "get_client"]
