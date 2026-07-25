"""Qwen Payload Builder — formats chat completions JSON body."""

import time
import uuid
from engine.config import MODEL, get_model_config, get_thinking_mode_config


def build_body(
    message: str,
    chat_id: str,
    parent_id: str | None,
    files: list[dict] | None = None,
    model: str | None = None,
    thinking_mode: str | None = None,
) -> dict:
    """Build the request payload for Qwen chat completions endpoint.

    `model`, if given, selects which entry from config.MODELS to use (falls
    back to the default MODEL). `thinking_mode`, if given, selects which of
    that model's supported thinking modes to use (e.g. "fast", "auto",
    "thinking") — falls back to the model's first/default mode if omitted or
    unsupported by that model (e.g. qwen3.7-max has no "auto" mode).
    thinking_enabled/auto_thinking/thinking_mode are all pulled from that
    selected mode's config rather than hardcoded.
    """
    now = int(time.time())
    file_list = files or []

    model_cfg = get_model_config(model)
    model_id = model_cfg["id"]
    mode_cfg = get_thinking_mode_config(model, thinking_mode)

    return {
        "stream": True,
        "version": "2.1",
        "incremental_output": True,
        "chatId": chat_id,
        "parentId": parent_id or "",
        "chat_id": chat_id,
        "chat_mode": "normal",
        "model": model_id,
        "parent_id": parent_id,
        "messages": [
            {
                "id": None,
                "fid": str(uuid.uuid4()),
                "parentId": parent_id,
                "childrenIds": [],
                "role": "user",
                "content": message,
                "user_action": "chat",
                "files": file_list,
                "timestamp": now,
                "models": [model_id],
                "model": "",
                "chat_type": "t2t",
                "feature_config": {
                    "thinking_enabled": mode_cfg["thinking_enabled"],
                    "output_schema": "phase",
                    "research_mode": "normal",
                    "auto_thinking": mode_cfg["auto_thinking"],
                    "thinking_mode": mode_cfg["thinking_mode"],
                    "thinking_format": "summary",
                    "auto_search": True,
                },
                "extra": {"meta": {"subChatType": "t2t"}},
                "sub_chat_type": "t2t",
                "parent_id": parent_id,
            }
        ],
        "timestamp": now,
    }