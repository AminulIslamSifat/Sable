"""OpenAI API connector for Sable — thin shim over the unified OpenAI-compat client."""

from __future__ import annotations
from engine.config import PERSISTENT_ROOT  # frozen-build writable root

from pathlib import Path

from connectors.common.openai_compat import OpenAICompatClient

_SYSTEM_DIR = PERSISTENT_ROOT / "system"


class OpenAIClient(OpenAICompatClient):
    NAME = "OpenAI"
    BASE_URL = "https://api.openai.com/v1"
    KEYS_PATH = _SYSTEM_DIR / ".openai_api_keys.json"
    DEFAULT_MODEL = "gpt-4o"
    INSTRUCTION_MODE = "minimal"   # 8k TPM limit, no persona injection
    ENABLE_REASONING = False


_client: OpenAIClient | None = None


def get_client() -> OpenAIClient:
    """Return the global OpenAIClient singleton."""
    global _client
    if _client is None:
        _client = OpenAIClient()
    return _client
