"""Integration test for DeepSeek connector with mocked HTTP layer.

Simulates the full stream_chat flow:
  - Per-chat session creation
  - Hello seed (first message)
  - Chained subsequent messages
  - Token rotation on failure
  - Empty response handling
  - SSE event parsing

Run: uv run python tests/test_deepseek_connector.py
     or: python -m pytest tests/test_deepseek_connector.py -v
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Fake SSE stream builder
# ---------------------------------------------------------------------------

def _build_sse_lines(
    text: str,
    message_id: int = 42,
    thinking: str | None = None,
    biz_error: bool = False,
) -> list[str]:
    """Build raw SSE lines that mimic DeepSeek's /chat/completion stream."""
    lines: list[str] = []

    if biz_error:
        lines.append(f'data: {json.dumps({"biz_code": 1, "biz_msg": "account muted"})}')
        lines.append("data: [DONE]")
        return lines

    # Handshake: response_message_id
    lines.append(f'data: {json.dumps({"request_message_id": 1, "response_message_id": message_id, "model_type": "default"})}')

    # Thinking fragments
    if thinking:
        lines.append(f'data: {json.dumps({"v": [{"type": "THINK", "content": ""}], "p": "response/fragments", "o": "APPEND"})}')
        # Send thinking char by char in chunks
        chunk_size = max(1, len(thinking) // 3)
        for i in range(0, len(thinking), chunk_size):
            chunk = thinking[i : i + chunk_size]
            lines.append(f'data: {json.dumps({"v": chunk, "p": "response/fragments/-1/content"})}')

    # Answer fragments — split into chunks to simulate streaming
    lines.append(f'data: {json.dumps({"v": [{"type": "RESPONSE", "content": ""}], "p": "response/fragments", "o": "APPEND"})}')
    chunk_size = max(1, len(text) // 4)
    for i in range(0, len(text), chunk_size):
        chunk = text[i : i + chunk_size]
        lines.append(f'data: {json.dumps({"v": chunk, "p": "response/fragments/-1/content"})}')

    # FINISHED
    lines.append(f'data: {json.dumps({"v": "FINISHED", "p": "response/status", "o": "SET"})}')
    lines.append("data: [DONE]")
    return lines


class FakeStreamResponse:
    """Mimics curl_cffi/httpx async streaming response."""

    def __init__(self, status_code: int, sse_lines: list[str]):
        self.status_code = status_code
        self._lines = sse_lines
        self.content = b"error body"

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class FakeHttpSession:
    """Mimics curl_cffi AsyncSession with controllable responses."""

    def __init__(self):
        self.responses: list[Any] = []
        self._call_idx = 0
        self.post_calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []

    def add_response(self, resp: Any):
        self.responses.append(resp)

    def _next_response(self) -> Any:
        if self._call_idx < len(self.responses):
            resp = self.responses[self._call_idx]
            self._call_idx += 1
            return resp
        raise RuntimeError(f"FakeHttpSession: no more responses (called {self._call_idx} times)")

    async def post(self, url: str, **kwargs) -> Any:
        self.post_calls.append({"url": url, **kwargs})
        resp = self._next_response()
        return resp

    def stream(self, method: str, url: str, **kwargs) -> Any:
        self.stream_calls.append({"method": method, "url": url, **kwargs})
        return self._next_response()


class FakeJsonResponse:
    """Mimics a non-streaming JSON response (for session create, challenge)."""

    def __init__(self, data: dict, status_code: int = 200):
        self._data = data
        self.status_code = status_code

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

_results: list[tuple[str, bool, str]] = []


def _record(name: str, passed: bool, detail: str = ""):
    _results.append((name, passed, detail))
    status = "✅ PASS" if passed else "❌ FAIL"
    print(f"  {status}  {name}" + (f" — {detail}" if detail and not passed else ""))


# Patch jitter and sleep globally for all tests so nothing actually waits
import connectors.deepseek.client as _ds_mod
import connectors.common.jitter as _jitter_mod

async def _instant_sleep(delay: float) -> None:
    """No-op sleep for tests."""
    pass

_ds_mod.asyncio.sleep = _instant_sleep
_jitter_mod.asyncio.sleep = _instant_sleep

# Force auto-rotation enabled for tests (real settings may disable it)
_ds_mod._auto_rotate_enabled = lambda provider: True


async def _make_client(fake_http: FakeHttpSession) -> Any:
    """Create a DeepSeekClient with all external deps mocked out."""
    from connectors.deepseek.client import DeepSeekClient

    client = DeepSeekClient.__new__(DeepSeekClient)
    client._token = "fake-jwt-token-1234567890abcdef"
    client._account = "test-account"
    client._http = fake_http
    client._sessions: dict[str, list[dict[str, Any]]] = {}
    client._parent_ids: dict[str, int | None] = {}
    client._rotate_tokens = ["fake-jwt-token-1234567890abcdef"]
    client._rotate_idx = 0
    client._chat_sessions: dict[tuple[str, str], str] = {}
    client._last_prepare_error = None
    client._max_session_chars = 100_000
    client._instruction_cache: dict[str, str] = {}
    return client


async def test_first_message_hello_seed_flow():
    """First message should: create session → hello seed → chain real message."""
    name = "first_message_hello_seed_flow"
    fake_http = FakeHttpSession()

    # Response queue:
    # 1. _create_session POST → returns session UUID
    fake_http.add_response(FakeJsonResponse({
        "code": 0,
        "data": {"biz_data": {"chat_session": {"id": "sess-uuid-001"}}},
    }))
    # 2. _get_challenge POST → returns challenge
    fake_http.add_response(FakeJsonResponse({
        "code": 0,
        "data": {"biz_data": {"challenge": "fake-challenge-string"}},
    }))
    # 3. _seed_hello_message stream → returns message_id=100
    hello_sse = _build_sse_lines("Hi!", message_id=100)
    fake_http.add_response(FakeStreamResponse(200, hello_sse))
    # 4. Real message stream → returns message_id=101
    real_sse = _build_sse_lines("Hello! I am DeepSeek.", message_id=101)
    fake_http.add_response(FakeStreamResponse(200, real_sse))

    client = await _make_client(fake_http)

    # Mock PoW solver to avoid needing the Go binary
    client._solve_pow = MagicMock(return_value="fake-nonce-abc")
    client._build_pow_header = MagicMock(return_value="fake-pow-header")

    events: list[dict[str, Any]] = []
    async for event in client.stream_chat(
        "What is 2+2?",
        chat_id="chat-test-1",
        inject_instructions=True,
        system_instruction="You are a helpful assistant.",
    ):
        events.append(event)

    # Verify we got answer events
    answer_texts = [e["text"] for e in events if e.get("type") == "answer"]
    full_answer = "".join(answer_texts)
    _record(name, full_answer == "Hello! I am DeepSeek.", f"got: {full_answer!r}")

    # Verify done event has parent_id=101
    done_events = [e for e in events if e.get("type") == "done"]
    _record(f"{name}_parent_id", len(done_events) == 1 and done_events[0]["parent_id"] == 101,
            f"done events: {done_events}")

    # Verify session was created
    session_posts = [c for c in fake_http.post_calls if "chat_session/create" in c["url"]]
    _record(f"{name}_session_created", len(session_posts) == 1)

    # Verify hello seed was sent (stream call #1)
    stream_calls = fake_http.stream_calls
    _record(f"{name}_two_stream_calls", len(stream_calls) == 2, f"got {len(stream_calls)}")

    # First stream call should be the hello seed
    if len(stream_calls) >= 1:
        hello_body = stream_calls[0].get("json", {})
        _record(f"{name}_hello_prompt", hello_body.get("prompt") == "hello",
                f"prompt={hello_body.get('prompt')!r}")
        _record(f"{name}_hello_model_type", hello_body.get("model_type") == "default",
                f"model_type={hello_body.get('model_type')!r}")
        _record(f"{name}_hello_parent_null", hello_body.get("parent_message_id") is None)

    # Second stream call should be the real message chained to hello's message_id
    if len(stream_calls) >= 2:
        real_body = stream_calls[1].get("json", {})
        _record(f"{name}_real_parent_chained", real_body.get("parent_message_id") == 100,
                f"parent={real_body.get('parent_message_id')}")
        _record(f"{name}_real_model_type_null", real_body.get("model_type") is None,
                f"model_type={real_body.get('model_type')!r}")
        prompt = real_body.get("prompt", "")
        _record(f"{name}_real_has_instructions", "helpful assistant" in prompt.lower(),
                f"prompt starts with: {prompt[:80]!r}")
        _record(f"{name}_real_has_user_msg", "What is 2+2?" in prompt)

    # Verify parent_id stored for next turn
    _record(f"{name}_parent_stored", client._parent_ids.get("chat-test-1") == 101,
            f"stored: {client._parent_ids.get('chat-test-1')}")

    # Verify per-chat session cached
    cache_key = ("fake-jwt-token-1234567890abcdef", "chat-test-1")
    _record(f"{name}_session_cached", client._chat_sessions.get(cache_key) == "sess-uuid-001")


async def test_subsequent_message_chaining():
    """Second message should skip hello seed and chain directly."""
    name = "subsequent_message_chaining"
    fake_http = FakeHttpSession()

    # Pre-populate state as if first message already happened
    client = await _make_client(fake_http)
    client._parent_ids["chat-test-2"] = 101
    client._chat_sessions[("fake-jwt-token-1234567890abcdef", "chat-test-2")] = "sess-uuid-002"
    client._sessions["chat-test-2"] = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello!"},
    ]
    client._solve_pow = MagicMock(return_value="nonce")
    client._build_pow_header = MagicMock(return_value="pow-hdr")

    # Only need challenge + one stream response (no session create, no hello)
    fake_http.add_response(FakeJsonResponse({
        "code": 0, "data": {"biz_data": {"challenge": "chal"}},
    }))
    reply_sse = _build_sse_lines("The answer is 4.", message_id=102)
    fake_http.add_response(FakeStreamResponse(200, reply_sse))

    events: list[dict[str, Any]] = []
    async for event in client.stream_chat("What is 2+2?", chat_id="chat-test-2"):
        events.append(event)

    answer = "".join(e["text"] for e in events if e.get("type") == "answer")
    _record(name, answer == "The answer is 4.", f"got: {answer!r}")

    # Should NOT have created a new session
    session_posts = [c for c in fake_http.post_calls if "chat_session/create" in c.get("url", "")]
    _record(f"{name}_no_new_session", len(session_posts) == 0)

    # Should have exactly ONE stream call (no hello seed)
    _record(f"{name}_one_stream_call", len(fake_http.stream_calls) == 1,
            f"got {len(fake_http.stream_calls)}")

    if fake_http.stream_calls:
        body = fake_http.stream_calls[0].get("json", {})
        _record(f"{name}_chained_to_101", body.get("parent_message_id") == 101,
                f"parent={body.get('parent_message_id')}")
        _record(f"{name}_model_type_null", body.get("model_type") is None)
        # Prompt should contain the user message (reminder may be prepended)
        prompt = body.get("prompt", "")
        _record(f"{name}_prompt_has_user_msg", "What is 2+2?" in prompt, f"prompt={prompt[:80]!r}")
        # Should NOT have system instruction dump (only first message gets that)
        _record(f"{name}_no_instruction_dump", "You are helpful" not in prompt)

    _record(f"{name}_parent_updated", client._parent_ids.get("chat-test-2") == 102)


async def test_thinking_mode():
    """Thinking fragments should be yielded as type='thinking'."""
    name = "thinking_mode"
    fake_http = FakeHttpSession()
    client = await _make_client(fake_http)
    client._parent_ids["chat-think"] = 50
    client._chat_sessions[("fake-jwt-token-1234567890abcdef", "chat-think")] = "sess-think"
    client._solve_pow = MagicMock(return_value="n")
    client._build_pow_header = MagicMock(return_value="p")

    fake_http.add_response(FakeJsonResponse({
        "code": 0, "data": {"biz_data": {"challenge": "c"}},
    }))
    sse = _build_sse_lines("Final answer.", message_id=51, thinking="Let me think...")
    fake_http.add_response(FakeStreamResponse(200, sse))

    events: list[dict[str, Any]] = []
    async for event in client.stream_chat("Think hard", chat_id="chat-think", thinking_mode="thinking"):
        events.append(event)

    thinking_events = [e for e in events if e.get("type") == "thinking"]
    thinking_text = "".join(e["text"] for e in thinking_events)
    _record(name, "Let me think..." in thinking_text, f"thinking: {thinking_text!r}")

    answer = "".join(e["text"] for e in events if e.get("type") == "answer")
    _record(f"{name}_answer", answer == "Final answer.", f"got: {answer!r}")

    # Verify thinking_enabled was True in the body
    if fake_http.stream_calls:
        body = fake_http.stream_calls[0].get("json", {})
        _record(f"{name}_flag_set", body.get("thinking_enabled") is True)


async def test_biz_error_rotation():
    """biz_error should trigger token rotation, not infinite retry."""
    name = "biz_error_rotation"
    fake_http = FakeHttpSession()

    # Two tokens in pool
    client = await _make_client(fake_http)
    client._rotate_tokens = ["token-aaa-1111111111111111", "token-bbb-2222222222222222"]
    client._rotate_idx = 0
    client._chat_sessions = {}
    client._solve_pow = MagicMock(return_value="n")
    client._build_pow_header = MagicMock(return_value="p")

    # Token 1:
    # 1. session create POST
    fake_http.add_response(FakeJsonResponse({
        "code": 0, "data": {"biz_data": {"chat_session": {"id": "sess-t1"}}},
    }))
    # 2. challenge POST
    fake_http.add_response(FakeJsonResponse({
        "code": 0, "data": {"biz_data": {"challenge": "c1"}},
    }))
    # 3. hello seed stream → biz_error
    biz_sse = _build_sse_lines("", message_id=0, biz_error=True)
    fake_http.add_response(FakeStreamResponse(200, biz_sse))
    # 4. fallback: send real message as first msg (hello failed) → also biz_error
    #    (this triggers retry loop, then rotation)
    biz_sse_2 = _build_sse_lines("", message_id=0, biz_error=True)
    fake_http.add_response(FakeStreamResponse(200, biz_sse_2))

    # After exhausting retries on token 1, rotation happens → token 2:
    # 5. session create POST
    fake_http.add_response(FakeJsonResponse({
        "code": 0, "data": {"biz_data": {"chat_session": {"id": "sess-t2"}}},
    }))
    # 6. challenge POST
    fake_http.add_response(FakeJsonResponse({
        "code": 0, "data": {"biz_data": {"challenge": "c2"}},
    }))
    # 7. hello seed stream (token 2)
    hello_sse = _build_sse_lines("Hi", message_id=200)
    fake_http.add_response(FakeStreamResponse(200, hello_sse))
    # 8. real message stream (token 2)
    real_sse = _build_sse_lines("Recovered!", message_id=201)
    fake_http.add_response(FakeStreamResponse(200, real_sse))

    events: list[dict[str, Any]] = []
    async for event in client.stream_chat("Test", chat_id="chat-biz"):
        events.append(event)

    # Should have gotten a token_rotation event
    rotations = [e for e in events if e.get("type") == "token_rotation"]
    _record(name, len(rotations) == 1, f"rotations: {len(rotations)}")

    if rotations:
        _record(f"{name}_rotation_fields", rotations[0].get("new_chat") is True)

    # Should eventually succeed with token 2
    answer = "".join(e["text"] for e in events if e.get("type") == "answer")
    _record(f"{name}_recovered", "Recovered!" in answer, f"got: {answer!r}")

    # Verify we rotated to index 1
    _record(f"{name}_idx_advanced", client._rotate_idx == 1, f"idx={client._rotate_idx}")


async def test_empty_response_retry():
    """Empty response (HTTP 200 but no content) should retry then rotate."""
    name = "empty_response_retry"
    fake_http = FakeHttpSession()
    client = await _make_client(fake_http)
    client._rotate_tokens = ["token-xxx-1111111111111111"]
    client._rotate_idx = 0
    client._chat_sessions = {}
    client._solve_pow = MagicMock(return_value="n")
    client._build_pow_header = MagicMock(return_value="p")

    # Session create + challenge + empty stream (will fail since only 1 token, 8 retries)
    # We'll just do enough for it to exhaust and error out
    fake_http.add_response(FakeJsonResponse({
        "code": 0, "data": {"biz_data": {"chat_session": {"id": "sess-empty"}}},
    }))
    fake_http.add_response(FakeJsonResponse({
        "code": 0, "data": {"biz_data": {"challenge": "c"}},
    }))
    # 8 empty responses (MAX_RETRIES_PER_TOKEN)
    for _ in range(8):
        empty_sse = ["data: [DONE]"]
        fake_http.add_response(FakeStreamResponse(200, empty_sse))

    events: list[dict[str, Any]] = []
    async for event in client.stream_chat("Test", chat_id="chat-empty"):
        events.append(event)

    errors = [e for e in events if e.get("type") == "error"]
    _record(name, len(errors) >= 1, f"errors: {len(errors)}")

    retries = [e for e in events if e.get("type") == "status" and "retry" in e.get("message", "")]
    _record(f"{name}_retries_happened", len(retries) == 7, f"retry events: {len(retries)}")


async def test_per_chat_session_isolation():
    """Different chat_ids should get different DeepSeek sessions."""
    name = "per_chat_session_isolation"
    fake_http = FakeHttpSession()
    client = await _make_client(fake_http)
    client._solve_pow = MagicMock(return_value="n")
    client._build_pow_header = MagicMock(return_value="p")

    # Chat A: session create + challenge + hello + real
    fake_http.add_response(FakeJsonResponse({
        "code": 0, "data": {"biz_data": {"chat_session": {"id": "sess-A"}}},
    }))
    fake_http.add_response(FakeJsonResponse({
        "code": 0, "data": {"biz_data": {"challenge": "c"}},
    }))
    fake_http.add_response(FakeStreamResponse(200, _build_sse_lines("hi", message_id=300)))
    fake_http.add_response(FakeStreamResponse(200, _build_sse_lines("Answer A", message_id=301)))

    # Chat B: session create + challenge + hello + real (DIFFERENT session)
    fake_http.add_response(FakeJsonResponse({
        "code": 0, "data": {"biz_data": {"chat_session": {"id": "sess-B"}}},
    }))
    fake_http.add_response(FakeJsonResponse({
        "code": 0, "data": {"biz_data": {"challenge": "c"}},
    }))
    fake_http.add_response(FakeStreamResponse(200, _build_sse_lines("hi", message_id=400)))
    fake_http.add_response(FakeStreamResponse(200, _build_sse_lines("Answer B", message_id=401)))

    events_a: list[dict[str, Any]] = []
    async for event in client.stream_chat("Q1", chat_id="chat-A", system_instruction="Sys A"):
        events_a.append(event)

    events_b: list[dict[str, Any]] = []
    async for event in client.stream_chat("Q2", chat_id="chat-B", system_instruction="Sys B"):
        events_b.append(event)

    ans_a = "".join(e["text"] for e in events_a if e.get("type") == "answer")
    ans_b = "".join(e["text"] for e in events_b if e.get("type") == "answer")
    _record(name, ans_a == "Answer A" and ans_b == "Answer B", f"A={ans_a!r}, B={ans_b!r}")

    # Verify different sessions were cached
    key_a = ("fake-jwt-token-1234567890abcdef", "chat-A")
    key_b = ("fake-jwt-token-1234567890abcdef", "chat-B")
    _record(f"{name}_different_sessions",
            client._chat_sessions.get(key_a) == "sess-A" and client._chat_sessions.get(key_b) == "sess-B")

    # Verify independent parent chains
    _record(f"{name}_independent_parents",
            client._parent_ids.get("chat-A") == 301 and client._parent_ids.get("chat-B") == 401)


async def test_no_serialize_history_called():
    """Verify _serialize_history doesn't exist anymore (dead code removed)."""
    name = "no_serialize_history"
    from connectors.deepseek.client import DeepSeekClient
    _record(name, not hasattr(DeepSeekClient, "_serialize_history"))


async def test_no_build_switch_prompt():
    """Verify _build_switch_prompt doesn't exist anymore."""
    name = "no_build_switch_prompt"
    from connectors.deepseek.client import DeepSeekClient
    _record(name, not hasattr(DeepSeekClient, "_build_switch_prompt"))


async def test_no_maybe_summarize():
    """Verify _maybe_summarize doesn't exist anymore."""
    name = "no_maybe_summarize"
    from connectors.deepseek.client import DeepSeekClient
    _record(name, not hasattr(DeepSeekClient, "_maybe_summarize"))


async def test_sse_parser_directly():
    """Test _iter_completion_events parses SSE correctly in isolation."""
    name = "sse_parser"
    from connectors.deepseek.client import DeepSeekClient

    client = DeepSeekClient.__new__(DeepSeekClient)
    client._token = "t"
    client._account = "a"
    client._http = None
    client._sessions = {}
    client._parent_ids = {}
    client._rotate_tokens = []
    client._rotate_idx = 0
    client._chat_sessions = {}
    client._last_prepare_error = None
    client._max_session_chars = 100_000
    client._instruction_cache = {}

    sse_lines = _build_sse_lines("Hello world", message_id=999, thinking="hmm")
    fake_resp = FakeStreamResponse(200, sse_lines)

    events: list[dict[str, Any]] = []
    async for event in client._iter_completion_events(fake_resp):
        events.append(event)

    answers = [e for e in events if e.get("type") == "answer"]
    thinkings = [e for e in events if e.get("type") == "thinking"]
    dones = [e for e in events if e.get("type") == "done"]

    full_answer = "".join(e["text"] for e in answers)
    full_thinking = "".join(e["text"] for e in thinkings)

    _record(name, full_answer == "Hello world", f"answer: {full_answer!r}")
    _record(f"{name}_thinking", full_thinking == "hmm", f"thinking: {full_thinking!r}")
    _record(f"{name}_done", len(dones) == 1 and dones[0].get("parent_id") == 999)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    print("\n🧪 DeepSeek Connector Tests\n" + "=" * 50, flush=True)

    tests = [
        ("sse_parser", test_sse_parser_directly),
        ("dead_code_removed_1", test_no_serialize_history_called),
        ("dead_code_removed_2", test_no_build_switch_prompt),
        ("dead_code_removed_3", test_no_maybe_summarize),
        ("hello_seed_flow", test_first_message_hello_seed_flow),
        ("chaining", test_subsequent_message_chaining),
        ("thinking", test_thinking_mode),
        ("biz_error_rotation", test_biz_error_rotation),
        ("empty_response_retry", test_empty_response_retry),
        ("session_isolation", test_per_chat_session_isolation),
    ]

    for label, test_fn in tests:
        print(f"\n▸ {label}", flush=True)
        try:
            await asyncio.wait_for(test_fn(), timeout=5.0)
        except asyncio.TimeoutError:
            _record(label, False, "TIMEOUT (>5s)")
        except Exception as exc:
            _record(label, False, f"Exception: {exc}")
            import traceback
            traceback.print_exc()

    print("\n" + "=" * 50, flush=True)
    passed = sum(1 for _, p, _ in _results if p)
    failed = sum(1 for _, p, _ in _results if not p)
    total = len(_results)
    print(f"Results: {passed}/{total} passed, {failed} failed\n", flush=True)

    if failed:
        print("Failed tests:", flush=True)
        for n, p, d in _results:
            if not p:
                print(f"  ❌ {n}: {d}", flush=True)
        sys.exit(1)
    else:
        print("All tests passed! 🎉", flush=True)
        sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
