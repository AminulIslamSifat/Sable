"""OpenAI API connector for Sable — thin shim over the unified OpenAI-compat client."""

from __future__ import annotations

from pathlib import Path

from connectors.common.openai_compat import OpenAICompatClient

_SYSTEM_DIR = Path(__file__).resolve().parent.parent.parent / "system"


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
