#!/usr/bin/env python3
"""Probe: does DeepSeek, given the Study.md persona, produce the same
style of answer we saw from ChatGPT?

Runs the REAL study instruction builder with persona_override, then calls
the DeepSeek connector exactly like a study turn would.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from connectors.common.instruction_builder import build_instructions  # noqa: E402
from connectors.deepseek.client import get_client  # noqa: E402

INSTRUCTION_DIR = ROOT / "instruction"

QUESTION = (
    "can you explain this part\n\n"
    "> Two compound propositions p and q are logically equivalent if "
    "p↔q is a tautology."
)


def _read_persona(name: str) -> str:
    p = INSTRUCTION_DIR / f"{name}.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


async def run(persona_name: str) -> None:
    persona = _read_persona(persona_name)
    if not persona:
        print(f"!! persona {persona_name!r} not found")
        return

    system = build_instructions(
        provider="deepseek",
        layout_mode="study",
        persona_override=persona,
    )

    print(f"\n{'='*70}\nPERSONA: {persona_name}  |  system prompt: {len(system)} chars\n{'='*70}")

    client = get_client()
    if not client.is_available:
        print("!! DeepSeek client not available (no valid token)")
        return

    result = await client.chat(
        QUESTION,
        model=None,
        chat_id=f"probe-{persona_name}-{__import__('time').time()}",
        inject_instructions=True,
        system_instruction=system,
        layout_mode="study",
    )

    if result.get("error"):
        print(f"!! ERROR: {result['error']}")
        return

    print(result["answer"])


async def main() -> None:
    for name in ("Study", "Maria"):
        try:
            await run(name)
        except Exception as exc:  # noqa: BLE001
            print(f"!! {name} failed: {exc}")


if __name__ == "__main__":
    asyncio.run(main())