"""Shared provider config loader for scraper engines.

Replaces the duplicated config.py in each provider directory.
Each provider's config.py becomes a thin shim that imports from here
and declares provider-specific constants.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


class SimpleConsole:
    """Tiny Rich-compatible console that strips markup tags."""

    def print(self, *args: Any, **kwargs: Any) -> None:
        msg = " ".join(map(str, args))
        msg = re.sub(r"\[/?[a-z #0-9,]+\]", "", msg)
        print(msg, flush=True)

    def clear(self) -> None:
        print("\033[H\033[J", end="", flush=True)


console = SimpleConsole()


def load_provider_config(provider_dir: Path, default_platform: str) -> dict[str, Any]:
    """Load platforms.json and resolve active platform config.

    Args:
        provider_dir: Directory containing platforms.json
        default_platform: Fallback platform key (e.g. 'qwen', 'deepseek')

    Returns:
        Dict with keys: PLATFORMS_CONFIG, PLATFORM, SABLE_ROOT, PROJECT_ROOT,
        OUTPUT_ROOT, ASSETS_DIR, INSTRUCTIONS_DIR
    """
    sable_root = provider_dir.parents[2]
    project_root = sable_root

    config_path = provider_dir / "platforms.json"
    with open(config_path, "r", encoding="utf-8") as f:
        platforms_config = json.load(f)

    active_key = os.environ.get("GHOST_PLATFORM") or platforms_config.get("active_platform", default_platform)
    platform = platforms_config[active_key]

    output_root = os.environ.get("SABLE_SCRAPER_OUTPUT", str(Path.home() / "sable_output"))
    if output_root.startswith("~"):
        output_root = os.path.expanduser(output_root)
    elif not os.path.isabs(output_root):
        output_root = str(sable_root / output_root)

    assets_dir = os.path.join(output_root, "assets")
    instructions_dir = Path(os.environ.get("SABLE_INSTRUCTION_DIR", str(sable_root / "instruction")))

    # Ensure directories exist
    os.makedirs(assets_dir, exist_ok=True)
    os.makedirs(os.path.join(output_root, "notes"), exist_ok=True)
    os.makedirs(os.path.join(output_root, "sessions"), exist_ok=True)
    os.makedirs(os.path.join(output_root, "logs"), exist_ok=True)

    return {
        "PLATFORMS_CONFIG": platforms_config,
        "PLATFORM": platform,
        "SABLE_ROOT": sable_root,
        "PROJECT_ROOT": project_root,
        "OUTPUT_ROOT": output_root,
        "ASSETS_DIR": assets_dir,
        "INSTRUCTIONS_DIR": instructions_dir,
    }
