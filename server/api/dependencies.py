from __future__ import annotations

import json
from typing import Any

from engine.config import get_browser_data_dir, _resolve_active_account, get_provider_account
from engine.service import ChatService
from connectors.deepseek.client import get_client as get_deepseek_client
from connectors.zai.client import get_client as get_zai_client
from connectors.gemini_web.client import get_client as get_gemini_web_client
from connectors.mimo.client import get_client as get_mimo_client
from connectors.mistral_web.client import get_client as get_mistral_web_client

service = ChatService(user_data_dir=str(get_browser_data_dir()))
get_deepseek_client().set_token_refresher(service.refresh_deepseek_token)
get_zai_client().set_token_refresher(service.refresh_zai_token)
get_zai_client().set_page_provider(service.get_zai_page)
get_gemini_web_client().set_cookie_provider(service.get_gemini_cookies)
get_gemini_web_client().set_token_provider(service.get_gemini_at_token)
get_mimo_client().set_cookie_provider(service.get_mimo_cookies)
get_mistral_web_client().set_cookie_provider(service.get_mistral_web_cookies)

# Wire account resolvers so error messages include [provider | account] context.
# The resolver is provider-aware: a provider's own active-account slot wins
# over the global active account, matching what service._browser_for_provider()
# actually routes to.
def _provider_account(provider: str) -> str:
    """Resolve the account a provider is really using (pin wins over global)."""
    pinned = get_provider_account(provider)
    if pinned:
        return pinned
    return _resolve_active_account()


for _client, _prov in [
    (get_mistral_web_client(), "mistral_web"),
    (get_mimo_client(), "mimo"),
    (get_gemini_web_client(), "gemini_web"),
]:
    if hasattr(_client, "ctx"):
        _client.ctx.set_account_resolver(_provider_account, _prov)

def sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
#
