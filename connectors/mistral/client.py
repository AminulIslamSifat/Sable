"""Mistral AI API connector for Sable — thin shim over the unified OpenAI-compat client."""

from __future__ import annotations

from pathlib import Path

from connectors.common.openai_compat import OpenAICompatClient

_SYSTEM_DIR = Path(__file__).resolve().parent.parent.parent / "system"


class MistralClient(OpenAICompatClient):
    NAME = "Mistral"
    BASE_URL = "https://api.mistral.ai/v1"
    KEYS_PATH = _SYSTEM_DIR / ".mistral_api_keys.json"
    DEFAULT_MODEL = "mistral-large-latest"
    INSTRUCTION_MODE = "project"   # full persona + project-aware instructions
    ENABLE_REASONING = True        # supports reasoning_effort


_client: MistralClient | None = None


def get_client() -> MistralClient:
    """Return the global MistralClient singleton."""
    global _client
    if _client is None:
        _client = MistralClient()
    return _client
