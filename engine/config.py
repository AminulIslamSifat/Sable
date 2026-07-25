"""Qwen Engine Configuration — Endpoints, Models, and Default Constants."""

URL = "https://chat.qwen.ai/api/v2/chat/completions"
NEW_CHAT_URL = "https://chat.qwen.ai/api/v2/chats/new"

# Each model carries its own list of selectable "thinking modes" — some
# models only support one mode (e.g. qwen3.8-max-preview is Thinking-only),
# others support several (qwen3.7-max: Fast/Thinking, qwen3.7-plus:
# Fast/Auto/Thinking). Each thinking mode entry maps directly onto the
# feature_config fields the upstream API expects. Add/remove model or mode
# entries here to control what's selectable in the UI.
MODELS = [
    {
        "id": "qwen3.8-max-preview",
        "label": "Qwen3.8 Max Preview",
        "thinking_modes": [
            {
                "id": "thinking",
                "label": "Thinking",
                "thinking_enabled": True,
                "auto_thinking": False,
                "thinking_mode": "Thinking",
            },
        ],
    },
    {
        "id": "qwen3.7-max",
        "label": "Qwen3.7 Max",
        "thinking_modes": [
            {
                "id": "fast",
                "label": "Fast",
                "thinking_enabled": False,
                "auto_thinking": False,
                "thinking_mode": "Fast",
            },
            {
                "id": "thinking",
                "label": "Thinking",
                "thinking_enabled": True,
                "auto_thinking": False,
                "thinking_mode": "Thinking",
            },
        ],
    },
    {
        "id": "qwen3.7-plus",
        "label": "Qwen3.7 Plus",
        "thinking_modes": [
            {
                "id": "fast",
                "label": "Fast",
                "thinking_enabled": False,
                "auto_thinking": False,
                "thinking_mode": "Fast",
            },
            {
                "id": "auto",
                "label": "Auto",
                "thinking_enabled": True,
                "auto_thinking": True,
                "thinking_mode": "Auto",
            },
            {
                "id": "thinking",
                "label": "Thinking",
                "thinking_enabled": True,
                "auto_thinking": False,
                "thinking_mode": "Thinking",
            },
        ],
    },
]

# Default/current model id — kept for backward compatibility with code that
# imports MODEL directly (chat.py, session.py create_new_chat, etc.)
MODEL = MODELS[0]["id"]


def get_model_config(model_id: str | None = None) -> dict:
    """Return the model config dict for model_id, falling back to the default MODEL.

    If model_id isn't found in MODELS, falls back to the first entry rather
    than raising, so an unrecognized/legacy model string doesn't crash payload
    building — it just won't get thinking mode toggled correctly.
    """
    target = model_id or MODEL
    for entry in MODELS:
        if entry["id"] == target:
            return entry
    return MODELS[0]


def get_thinking_mode_config(model_id: str | None = None, thinking_mode_id: str | None = None) -> dict:
    """Return the selected thinking-mode config for a model.

    Falls back to that model's first (default) thinking mode if
    thinking_mode_id is missing or not supported by the model — e.g. the
    client requests "auto" on qwen3.7-max, which doesn't have that mode.
    """
    modes = get_model_config(model_id)["thinking_modes"]
    if thinking_mode_id:
        for mode in modes:
            if mode["id"] == thinking_mode_id:
                return mode
    return modes[0]

# Default fallback session tokens (auto-refreshed via Playwright)
COOKIES = (
    "cna=HJfoIqI66h4CAXazbrEYQsVS; qwen-theme=light; qwen-locale=en-US; "
    "isg=BCQktq5oEvWJk2a6RyikgTMu9isWvUgnvquJjD5Fdu-z6cCzYsmftlIPqdlxKoB_; "
    "tfstk=gB0j8c1o7W2zcDOvBtRyPzM9dAUw1Q8FH1NttfQV6rUYC1MZ9lFVuImsVAkbHqzY6gE-Q5iwiKcYCRNZLoQTDCY-PAkdXthvjTI_sWiNkSd0CTF_klm4Q-y_5fDIcp8e8jc0SrpeLeyMuIEakSFYMidT2WNpaSeEWUq8SPpeUEmOm44i3CbvDlhJN5PTMNHTX7e8E5_AHAeY2TFQtPetBAe82WNhHsQTBzQ8E5UT6AFvNbeu6PetBPdSwLQNP7buUjOZr5BE0NdY08_OW4N8iRh5nNNrlSi_LjINRwOgGowKM8vVo9eLAxEsSB_SFVw_bJqc9GUKxYg_vytwiyGS4DrjVKQ_Uqy7vqU6EgNrXX37v4dNiR0SwqIr-wyQclS1NkbTNiAWNGjMeXMA5yylvCqYZ7yeNQseGoFuNJRWNGjgD7Vl8QO5Yd1..; "
    "xlly_s=1; "
    "token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpZCI6ImUyMzJlZDhhLTAzYTItNGY2Ny1hMWYzLWFiNmRkOGUyZjM1NSIsImxhc3RfcGFzc3dvcmRfY2hhbmdlIjoxNzU1NTg3NDE1LCJleHAiOjE3ODc0MTA5Mzd9.b5eLsQfJzR2Lksm2WsQkyV9pr0pJNW82HfbQV2T3cGY; "
    "cnaui=e232ed8a-03a2-4f67-a1f3-ab6dd8e2f355; "
    "aui=e232ed8a-03a2-4f67-a1f3-ab6dd8e2f355; "
    "qwen-thinking_mode=Fast; "
    "acw_tc=0a06abd917848189304376213e3a1d8f31804a2725ab86675654a0430a17b4; "
    "x-ap=ap-southeast-1; "
    "sca=479bb08c; "
    "atpsida=d74828bcd9e3afceb5298ccc_1784818964_3"
)

BX_UA = (
    "231!ARp3fkmUyS3+jmS+c+7jZzSjUGf3YceqREw9p7tdjNI4xfS2s1UKPHap9jz36C0SVz/iS6EZnBE0UgW7Bl93eWYX5rlXG3bQkx9kHs8P3RhG1XdXI77mWi4O5XXZH0TIzBqtt25UlkaRtKBwy5AHDELppTZEiCc1iSPikQtQwOT8S2g6ETC9M8tByyZcx6liwFR2Ve8yEWW/NPSPIDW14046fmQdkrubxKhATyx0UPEUFVD+zu8r+kh++6WF1csSt7h7HJBh+++j+ygU3+jOvs4IRaeaFkk3kLXHBudEknCXbI6eozQTTjWW+FmbycHt/AW2Vz5j8T5fGfsnqYQ5Tlm8NjXXVp+qADRYGHFz5JeKR3csQHfrRLMdTKlhUnO3RwAJf92zNnb/SnFkHqNY0siAj2ghZBoEvOABWwtXE9wsDvvLhm5m2CqDwpTTJC5uhXU4FSzkJfn6nbtUE5DcQpno80Y0Z/4ylpijDqeCyGTcmoWYkn7PN8FE0kSYQTZE2YLHP/EZFTE/LQqkPOFmOflBN8aQW9E+FXrlM6CXQCgg/jEgAzX8g0nN2/oxz/C5GNTid1CEEM0TOGBStt1q1Db7zKYm3jxLE/L9vEqgPu+M1RN6qy2mvJIWDeV+LXwV4eRLDqB3Db8oS2zUK3Elc6uOggrcIL3zO0VL+rcEy9Xn1FVrs3UgqZ2sAfCgDYDv5PfU83IaSKzAUreYOku5+tlAjkM/w+Dan44gv+xPfuZPkxSHhnm62NHrZpD0U6HQRsp6/C7DiP6jehrgJVmbw3bORcOLdf1CMeQi5kQMYRmZ8rX/6DMI4g/T7tXNq5g4G0Yljdi/6w8hmJWCl54iDdBrVYUm2VYjuQYJBqsbav9XYEVXwCMqM8hqnp/gVoV2CBw+MEWJcl7AW8cu/8wNKrqsuEvEU9xLBYS+VB4Jr36D4M/6WInTyLDfe62duf9ERAsZ00REpN2ILXuXnh3NRtwFYFUlZ4uJg4gk+2ek0PfeNzRYzlFAAlRxyytUkZ/LSAKQwSIoqIohjUDb5h96C8HjXUnUpAwbku1kf8QPXRZPzm0MGd400LaMOm6z1eR1ECXSI1qyEmmWb6/wFk9SBYV5gjKTJagjUt/DeI0S5ciq6nCWkc+rybTnFt0SPCYNUzleEusUo20H3po2i7/q5DAkAq8HtSmD9U0kSMHXqohv1jEUHZ5cqaykiiKKIWbwoW3ms/6MsNLk+FPBumslBF+cr5b0+naq1T9H1QM4Ogkc8aEqVWcTfKn54E8gDmKSzxtiELTMq3/zSnDne9hUt45DXI6wVTtLmp33mNn7uPrTJbHwo3er26D9MStkUZ34/qgmeTJjjE1ITgBMmcECju/2s9/qdz+ZlLuUiNmyCs6gQsJDQco1pZq0mT6KWL5yvZIvmOdTn/I94qT8fmAjex7Nl2hwEHGgjEmSPC/Z0bxtertsbovO1OQ6EdegFb8licEZWmiiNz4XVNaRaLSmDieO99KFNilgUhY4ZoBoHSgqWETmSIrJG4BgptyIrRSW766CMyM4h/M0Sdkta/cks8++IcVEOI1KPufRejDpTv0LOKJEUS="
)

BX_UMIDTOKEN = "T2gAIZxoFFB3FTLt4DQzJp5WpWADR8wqlQH4C7wpzS0twSUNGmauTXk4lsW-vqZ6VZo="