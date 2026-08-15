"""Minimal config shim for the vendored Qwen browser scraper engine."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path


class SimpleConsole:
    """Tiny Rich-compatible console that strips markup tags."""

    def print(self, *args, **kwargs) -> None:
        msg = " ".join(map(str, args))
        msg = re.sub(r"\[/?[a-z #0-9,]+\]", "", msg)
        print(msg, flush=True)

    def clear(self) -> None:
        print("\033[H\033[J", end="", flush=True)


console = SimpleConsole()

PROJECT_ROOT = Path(__file__).parent.resolve()
CONFIG_DIR = PROJECT_ROOT

# Sable repository root: engine/scraper_engines/qwen -> parents[3]
SABLE_ROOT = PROJECT_ROOT.parents[2]

CONFIG_PATH = CONFIG_DIR / "platforms.json"
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    PLATFORMS_CONFIG = json.load(f)

active_key = os.environ.get("GHOST_PLATFORM") or PLATFORMS_CONFIG.get("active_platform", "qwen")
PLATFORM = PLATFORMS_CONFIG[active_key]

OUTPUT_ROOT = os.environ.get("SABLE_SCRAPER_OUTPUT", str(Path.home() / "sable_output"))
if OUTPUT_ROOT.startswith("~"):
    OUTPUT_ROOT = os.path.expanduser(OUTPUT_ROOT)
elif not os.path.isabs(OUTPUT_ROOT):
    OUTPUT_ROOT = str(SABLE_ROOT / OUTPUT_ROOT)

ASSETS_DIR = os.path.join(OUTPUT_ROOT, "assets")
INSTRUCTIONS_DIR = Path(os.environ.get("SABLE_INSTRUCTION_DIR", str(SABLE_ROOT / "instruction")))

os.makedirs(ASSETS_DIR, exist_ok=True)
os.makedirs(os.path.join(OUTPUT_ROOT, "notes"), exist_ok=True)
os.makedirs(os.path.join(OUTPUT_ROOT, "sessions"), exist_ok=True)
os.makedirs(os.path.join(OUTPUT_ROOT, "logs"), exist_ok=True)
