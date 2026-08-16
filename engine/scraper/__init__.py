"""engine.scraper — split into settings/loader/lifecycle/stream submodules.

All public names are re-exported here so existing imports like
`from engine.scraper import scraper, get_settings` continue to work unchanged.
"""

from .settings import (
    ENGINE_REGISTRY,
    DEFAULT_ENGINE_TYPE,
    DEFAULT_SETTINGS,
    SETTINGS_PATH,
    ENGINES_DIR,
    _resolve_engine_path,
    _load_settings,
    get_settings,
    list_engines,
    save_settings,
    update_settings,
)
from .loader import _load_py_module, _accepts_arg
from .stream import ScraperEngine, scraper

__all__ = [
    "ENGINE_REGISTRY",
    "DEFAULT_ENGINE_TYPE",
    "DEFAULT_SETTINGS",
    "SETTINGS_PATH",
    "ENGINES_DIR",
    "_resolve_engine_path",
    "_load_settings",
    "get_settings",
    "list_engines",
    "save_settings",
    "update_settings",
    "_load_py_module",
    "_accepts_arg",
    "ScraperEngine",
    "scraper",
]
