"""Standalone smoke test for the ChatGPT ghost connector.

Runs the FULL path (ghost session -> open_stream -> SSE decode) against a live
profile, prints every decoded answer event, and leaves the raw bridge capture
in /home/sifat/sable_output/logs/ for inspection.

    PYTHONPATH=. .venv/bin/python connectors/chatgpt/_smoke.py browser-data-acc5
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from connectors.chatgpt.client import ChatGPTClient


async def main() -> None:
    account = sys.argv[1] if len(sys.argv) > 1 else "browser-data-acc5"
    client = ChatGPTClient(account=account)
    print(f"account={account} available={client.is_available}")

    prompt = "count from 1 to 20, one number per line, nothing else"
    answer = []
    thinking = []
    err = None
    done_id = None

    async for ev in client.stream_chat(prompt, inject_instructions=False):
        t = ev.get("type")
        if t == "answer":
            answer.append(ev.get("text", ""))
            print("ANS:", repr(ev.get("text", "")))
        elif t == "thinking":
            thinking.append(ev.get("text", ""))
        elif t == "done":
            done_id = ev.get("chat_id")
            print("DONE:", done_id)
        elif t == "error":
            err = ev.get("message")
            print("ERR:", err)

    full = "".join(answer)
    print("\n===== SUMMARY =====")
    print("answer_events:", len(answer))
    print("answer_chars:", len(full))
    print("thinking_chars:", len("".join(thinking)))
    print("error:", err)
    print("--- captured answer ---")
    print(full[:2000])


if __name__ == "__main__":
    asyncio.run(main())
