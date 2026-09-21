"""Drive the DeepSeek client exactly like a first message and dump raw SSE."""
from __future__ import annotations

import asyncio
import json
import sys

sys.path.insert(0, ".")

from connectors.deepseek.client import get_client, BASE_URL  # noqa: E402


async def main() -> None:
    client = get_client()

    # Force a tiny fake instruction to avoid the 20k dump during debugging
    system_instruction = (
        "You are a helpful assistant. Reply concisely.\n"
        "This is a test instruction block to emulate the real dump."
    )

    print("=== token available:", bool(client.token))
    if not client.token:
        print("NO TOKEN — abort")
        return

    events = []
    async for ev in client.stream_chat(
        message="say hi in one word",
        chat_id=None,
        inject_instructions=False,
        system_instruction=system_instruction,
    ):
        events.append(ev)
        t = ev.get("type")
        if t in ("answer", "thinking"):
            print(f"  [{t}] {ev.get('text','')!r}")
        else:
            print(f"  [{t}] {ev}")

    print("=== total events:", len(events))


if __name__ == "__main__":
    asyncio.run(main())
