"""Mistral AI API connector for Sable — thin shim over the unified OpenAI-compat client."""

from __future__ import annotations
from engine.config import PERSISTENT_ROOT  # frozen-build writable root

from pathlib import Path

from connectors.common.openai_compat import OpenAICompatClient

_SYSTEM_DIR = PERSISTENT_ROOT / "system"


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
