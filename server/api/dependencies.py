from __future__ import annotations

import json
from typing import Any

from engine.config import get_browser_data_dir
from engine.service import ChatService
from connectors.deepseek.client import get_client as get_deepseek_client
from connectors.zai.client import get_client as get_zai_client
from connectors.gemini_web.client import get_client as get_gemini_web_client
from connectors.mimo.client import get_client as get_mimo_client

service = ChatService(user_data_dir=str(get_browser_data_dir()))
get_deepseek_client().set_token_refresher(service.refresh_deepseek_token)
get_zai_client().set_token_refresher(service.refresh_zai_token)
get_zai_client().set_page_provider(service.get_zai_page)
get_gemini_web_client().set_cookie_provider(service.get_gemini_cookies)
get_gemini_web_client().set_token_provider(service.get_gemini_at_token)
get_mimo_client().set_cookie_provider(service.get_mimo_cookies)

def sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
#
