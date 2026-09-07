"""Qwen Chat — Main entry point and clean CLI interface."""

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

import httpx

from engine.config import MODEL, MODELS, URL, get_model_config
from engine.payloads import build_body
from engine.session import BrowserManager, create_new_chat

# --- Raw response logger (Qwen only) ---
_LOG_DIR = Path(__file__).resolve().parent.parent / "output" / "qwen_raw"
_LOG_DIR.mkdir(parents=True, exist_ok=True)


def _log_raw_line(line: str) -> None:
    """Append a single raw SSE line to today's log file."""
    logfile = _LOG_DIR / f"{datetime.now():%Y-%m-%d}.txt"
    with open(logfile, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now():%H:%M:%S.%f}] {line}\n")


_CHUNK_LOG_DIR = _LOG_DIR.parent / "qwen_chunks"
_CHUNK_LOG_DIR.mkdir(parents=True, exist_ok=True)


def _log_stream_chunk(phase: str, content: str) -> None:
    """Log a single parsed streaming content delta to today's chunk log."""
    if not content:
        return
    logfile = _CHUNK_LOG_DIR / f"{datetime.now():%Y-%m-%d}.txt"
    with open(logfile, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now():%H:%M:%S.%f}] [{phase}] {content!r}\n")

# Initialize single persistent browser manager instance
bm = BrowserManager()


async def stream_chat(
    message: str,
    headers: dict[str, str],
    chat_id: str | None = None,
    parent_id: str | None = None,
    files: list[dict] | None = None,
    model: str | None = None,
    is_retry: bool = False,
) -> tuple[str | None, str | None]:
    """Send a message and stream the response token by token, returning updated (chat_id, parent_id)."""
    new_chat_id = chat_id
    if not new_chat_id:
        new_chat_id = await create_new_chat(headers, model=model)
        if not new_chat_id:
            if not is_retry:
                print("[DEBUG] Chat creation failed. Refreshing tokens on-demand via Playwright...")
                headers.update(await bm.get_fresh_headers())
                return await stream_chat(message, headers, chat_id, parent_id, files=files, model=model, is_retry=True)
            print("[ERROR] Cannot proceed without valid chat_id.")
            return None, None

    new_parent_id = parent_id
    chosen_response_id: str | None = None
    body = build_body(message, new_chat_id, parent_id, files=files, model=model)

    try:
        params = {"chat_id": new_chat_id}
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream("POST", URL, headers=headers, json=body, params=params) as res:
                print(f"[DEBUG] HTTP {res.status_code}")

                if res.status_code in (401, 403) and not is_retry:
                    print(f"[DEBUG] Received HTTP {res.status_code}. Refreshing tokens on-demand via Playwright...")
                    await res.aread()
                    headers.update(await bm.get_fresh_headers())
                    return await stream_chat(
                        message, headers, new_chat_id, parent_id, files=files, model=model, is_retry=True
                    )

                if res.status_code != 200:
                    raw = (await res.aread()).decode()[:500]
                    print(f"[ERROR] {raw}")
                    return new_chat_id, new_parent_id

                in_thinking = False
                in_answer = False
                buffer = ""
                line_count = 0

                async for chunk in res.aiter_bytes():
                    if not chunk:
                        continue
                    buffer += chunk.decode("utf-8", errors="replace")
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line_str = line.strip()
                        line_count += 1

                        # Log every raw SSE line before any parsing
                        _log_raw_line(line_str)

                        if not line_str.startswith("data: "):
                            if line_str:
                                try:
                                    err = json.loads(line_str)
                                    if err.get("success") is False:
                                        print(f"[ERROR] code: {err.get('data', {}).get('code')}")
                                        print(f"[ERROR] details: {json.dumps(err.get('data', {}).get('details'), indent=2)}")
                                except json.JSONDecodeError:
                                    pass
                            continue

                        try:
                            data = json.loads(line_str[6:])
                        except json.JSONDecodeError:
                            continue

                        created = data.get("response.created")
                        if isinstance(created, dict):
                            response_id = created.get("response_id")
                            if isinstance(response_id, str):
                                if created.get("response_index") == "0" or chosen_response_id is None:
                                    chosen_response_id = response_id
                                    new_parent_id = response_id

                        choices = data.get("choices", [])
                        if not choices:
                            continue

                        response_id = data.get("response_id")
                        if isinstance(response_id, str):
                            if chosen_response_id is None:
                                chosen_response_id = response_id
                                new_parent_id = response_id
                            elif response_id != chosen_response_id:
                                continue

                        delta = choices[0].get("delta", {})
                        phase = delta.get("phase", "")
                        content = delta.get("content", "")

                        # Stream Thinking phase
                        if phase in ("thinking_summary", "thinking"):
                            extra = delta.get("extra", {})
                            thoughts = extra.get("summary_thought", {}).get("content", [])
                            text = "".join(thoughts) if thoughts else content

                            if text:
                                _log_stream_chunk(phase, text)
                                if not in_thinking:
                                    sys.stdout.write("\n🧠 [Thinking]\n")
                                    in_thinking = True
                                sys.stdout.write(text)
                                sys.stdout.flush()

                        # Stream Answer phase
                        elif phase == "answer" and content:
                            _log_stream_chunk(phase, content)
                            if not in_answer:
                                if in_thinking:
                                    sys.stdout.write("\n\n💬 [Answer]\n")
                                in_answer = True
                            sys.stdout.write(content)
                            sys.stdout.flush()

                print(f"\n\n[DEBUG] total lines received: {line_count}")

    except httpx.ConnectError as e:
        print(f"[ERROR] Connection failed: {e}")
    except httpx.ReadTimeout:
        print("[ERROR] Timed out waiting for response (120s)")
    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}")

    print()
    return new_chat_id, new_parent_id


async def main() -> None:
    chat_id: str | None = None
    parent_id: str | None = None
    current_model = MODEL

    print(f"Qwen Chat — {current_model}")
    headers = await bm.get_fresh_headers()
    print("Session ready. Starting new conversation...\n")
    print("Commands: /image <path> [prompt], /model <id>, /models, quit\n")

    try:
        while True:
            try:
                user_input = input("> ")
            except (EOFError, KeyboardInterrupt):
                print("\nbye 💙")
                break

            stripped = user_input.strip()
            if stripped.lower() == "quit":
                print("bye 💙")
                break
            if not stripped:
                continue

            # List available models
            if stripped.lower() == "/models":
                for entry in MODELS:
                    marker = " (current)" if entry["id"] == current_model else ""
                    print(f"  {entry['id']} — {entry['label']}{marker}")
                continue

            # Switch model: /model <id>
            if stripped.lower().startswith("/model "):
                requested = stripped.split(" ", 1)[1].strip()
                cfg = get_model_config(requested)
                if cfg["id"] != requested:
                    print(f"[WARN] Unknown model '{requested}', falling back to {cfg['id']}")
                current_model = cfg["id"]
                # Switching models mid-conversation starts a fresh chat session
                # since the server associates a chat_id with a model at creation.
                chat_id, parent_id = None, None
                print(f"[DEBUG] Switched to {current_model} (new chat session)")
                continue

            # Handle image prompt command: /image <path> [prompt]
            if stripped.lower().startswith(("/image ", "/img ")):
                parts = stripped.split(" ", 2)
                img_path = parts[1]
                prompt = parts[2] if len(parts) > 2 else "What is in this image?"

                file_meta = await bm.upload_image(
                    img_path,
                    cookies=headers.get("Cookie"),
                    bx_ua=headers.get("bx-ua"),
                    bx_umidtoken=headers.get("bx-umidtoken"),
                )
                if not file_meta:
                    continue

                chat_id, parent_id = await stream_chat(
                    prompt, headers, chat_id, parent_id, files=[file_meta], model=current_model
                )
                print()
                continue

            chat_id, parent_id = await stream_chat(user_input, headers, chat_id, parent_id, model=current_model)
            print()
    finally:
        await bm.close()


if __name__ == "__main__":
    asyncio.run(main())