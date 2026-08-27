from __future__ import annotations

import json
from typing import Any

from engine.config import get_browser_data_dir
from engine.service import ChatService
from connectors.deepseek.client import get_client as get_deepseek_client

service = ChatService(user_data_dir=str(get_browser_data_dir()))
get_deepseek_client().set_token_refresher(service.refresh_deepseek_token)

def sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
#
