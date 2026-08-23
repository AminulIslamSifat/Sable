"""Groq API connector for Sable — thin shim over the unified OpenAI-compat client."""

from __future__ import annotations

from pathlib import Path

from connectors.common.openai_compat import OpenAICompatClient

_SYSTEM_DIR = Path(__file__).resolve().parent.parent.parent / "system"


class GroqClient(OpenAICompatClient):
    NAME = "Groq"
    BASE_URL = "https://api.groq.com/openai/v1"
    KEYS_PATH = _SYSTEM_DIR / ".groq_api_keys.json"
    DEFAULT_MODEL = "llama-3.3-70b-versatile"
    INSTRUCTION_MODE = "minimal"   # tight prompt budget, no persona injection
    ENABLE_REASONING = False


_client: GroqClient | None = None


def get_client() -> GroqClient:
    """Return the global GroqClient singleton."""
    global _client
    if _client is None:
        _client = GroqClient()
    return _client
