"""Dynamic module loading utilities for scraper engines."""

from __future__ import annotations

import importlib.util
import inspect
import sys
import types
from pathlib import Path
from typing import Any


def _load_py_module(module_name: str, path: Path) -> types.ModuleType:
    """Load a Python file as a module by path."""
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _accepts_arg(func: Any, name: str) -> bool:
    """Check if a callable accepts a keyword argument by name."""
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return False

    for param in sig.parameters.values():
        if param.name == name:
            return True
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            return True
    return False
