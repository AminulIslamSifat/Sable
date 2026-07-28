
---
title: DeepSeek PoW — Fully Cracked Algorithm & Integration Guide
date: 2026-07-28
type: reference
tags: [deepseek, pow, scraper, reverse-engineering]
status: active
---

# DeepSeek PoW — Fully Cracked Algorithm & Integration Guide

## TL;DR

DeepSeek's chat API requires a Proof-of-Work challenge solved via a **custom SHA3-256 variant** (Keccak-f[1600] with round 0 skipped, 23 rounds instead of 24). The input is a plain string `salt_expireAt_nonce`, and the goal is to find a nonce whose hash **exactly equals** the server-provided challenge hash. Pure Python solves in ~50s; WASM/Go solves in ~50-100ms.

***

## The Algorithm (DeepSeekHashV1)

### Core Modification

Standard SHA3-256 uses Keccak-f[1600] with **24 rounds** (indices 0–23). DeepSeek's variant **skips round 0**, using only rounds 1–23 (23 rounds total). Everything else (sponge construction, rate=136, padding 0x06...0x80, 256-bit output) is identical to standard SHA3-256.

### Input Format

~~~python
input_bytes = f"{salt}_{expire_at}_{nonce}".encode()
# Example: b"0be7882557bb65a2fa49_1785245277105_42076"
# All plain ASCII string — NO hex decoding of anything
~~~

### Verification

~~~python
hash_output = deepseek_hash_v1(input_bytes)  # 32 bytes
target = bytes.fromhex(challenge_hex_string)  # 32 bytes
assert hash_output == target  # EXACT equality
~~~

The server generates challenges by picking a random nonce in `[0, difficulty)`, computing the hash, and sending it. Client brute-forces the same range.

***

## API Flow (3 Steps)

### Step 1: Get Challenge

~~~
POST https://chat.deepseek.com/api/v0/chat/create_pow_challenge
Headers: Authorization: Bearer <token>, Content-Type: application/json
Body: {"target_path": "/api/v0/chat/completion"}
~~~

Response:
~~~json
{
  "data": {
    "biz_data": {
      "challenge": {
        "algorithm": "DeepSeekHashV1",
        "challenge": "fd94a63d3fe9f2f64235ae9fe4692d3057529d87cd56572ea89ae4d7b7626f58",
        "salt": "0be7882557bb65a2fa49",
        "signature": "1b156203b748dd9f8a971c56a2ca698eb491936ad6f478b3d9274bff0f99bda3",
        "difficulty": 144000,
        "expire_at": 1785245277105,
        "expire_after": 300000,
        "target_path": "/api/v0/chat/completion"
      }
    }
  }
}
~~~

### Step 2: Solve & Encode PoW Header

~~~python
result = {
    "algorithm": "DeepSeekHashV1",
    "challenge": challenge_hex,
    "salt": salt,
    "answer": nonce_integer,
    "signature": signature,
    "target_path": "/api/v0/chat/completion"
}
pow_header = base64.b64encode(json.dumps(result, separators=(',',':')).encode()).decode()
~~~

### Step 3: Send Completion

~~~
POST https://chat.deepseek.com/api/v0/chat/completion
Headers:
  Authorization: Bearer <token>
  Content-Type: application/json
  X-DS-PoW-Response: <pow_header>
  x-client-version: 2.3.0
  x-client-platform: web
  x-client-bundle-id: com.deepseek.chat
  x-client-locale: en_US
Body: {
  "chat_session_id": "<uuid>",
  "parent_message_id": null,
  "model_type": "default",
  "prompt": "your message",
  "ref_file_ids": [],
  "thinking_enabled": false,
  "search_enabled": false,
  "action": null,
  "preempt": false
}
~~~

Response is SSE stream. Session created via `POST /api/v0/chat_session/create` (returns `data.biz_data.id`).

***

## Working Python Implementation (Verified)

~~~python
"""DeepSeekHashV1: SHA3-256 with Keccak-f[1600] rounds 1..23 (round 0 skipped)."""
import struct

RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]
M = 0xFFFFFFFFFFFFFFFF
PI  = [0, 6,12,18,24, 3, 9,10,16,22, 1, 7,13,19,20, 4, 5,11,17,23, 2, 8,14,15,21]
RHO = [0,44,43,21,14,28,20, 3,45,61, 1, 6,25, 8,18,27,36,10,15,56,62,55,39,41, 2]

def _rotl(v: int, k: int) -> int:
    return ((v << k) | (v >> (64 - k))) & M

def _keccak_f23(s: list[int]) -> None:
    a = list(s)
    for r in range(1, 24):  # SKIP ROUND 0 — this is the only modification
        c = [(a[i] ^ a[i+5] ^ a[i+10] ^ a[i+15] ^ a[i+20]) & M for i in range(5)]
        d = [(c[(i+4) % 5] ^ _rotl(c[(i+1) % 5], 1)) & M for i in range(5)]
        for i in range(5):
            for j in range(0, 25, 5):
                a[i+j] = (a[i+j] ^ d[i]) & M
        b = [_rotl(a[PI[i]], RHO[i]) for i in range(25)]
        for j in range(5):
            for i in range(5):
                a[j*5+i] = (b[j*5+i] ^ ((~b[j*5+(i+1) % 5] & M) & b[j*5+(i+2) % 5])) & M
        a[0] = (a[0] ^ RC[r]) & M
    for i in range(25):
        s[i] = a[i]

def deepseek_hash_v1(data: bytes) -> bytes:
    rate = 136
    s = [0] * 25
    off = 0
    while off + rate <= len(data):
        for i in range(rate // 8):
            s[i] ^= struct.unpack_from('<Q', data, off + i * 8)[0]
        _keccak_f23(s)
        off += rate
    buf = bytearray(rate)
    rem = len(data) - off
    buf[:rem] = data[off:]
    buf[rem] = 0x06
    buf[rate - 1] |= 0x80
    for i in range(rate // 8):
        s[i] ^= struct.unpack_from('<Q', buf, i * 8)[0]
    _keccak_f23(s)
    out = bytearray(32)
    for i in range(4):
        struct.pack_into('<Q', out, i * 8, s[i])
    return bytes(out)

def solve_pow(challenge_data: dict) -> int:
    """Brute-force solve. Returns nonce or -1."""
    prefix = f"{challenge_data['salt']}_{challenge_data['expire_at']}_".encode()
    target = bytes.fromhex(challenge_data["challenge"])
    for nonce in range(challenge_data["difficulty"]):
        if deepseek_hash_v1(prefix + str(nonce).encode()) == target:
            return nonce
    return -1
~~~

### Verified Test Vector

~~~
Input:  b"7650f5b27da8c1ab6cef_1785244726564_42076"
Output: 1667afccd601c7ad4df9b9231e7fe3274f12941a6613c4f672ec0222bf777b2a
Match:  True (verified against WASM solver output)
~~~

***

## Speed Optimization Options

| Backend | Speed | Dependency | Notes |
|:---|---:|:---|:---|
| Go binary | ~50ms | compile once | Best for production |
| WASM (wasmtime) | ~100ms | `pip install wasmtime` | Use their own WASM file |
| C (custom Keccak) | ~30ms | gcc | Fastest possible |
| Pure Python | ~50s | nothing | Too slow for real-time |

### WASM Approach (Recommended for Sable)

The WASM file is already downloaded at `/tmp/sha3_wasm.wasm` (also cached in browser-scraper-data). Call via wasmtime:

~~~python
import wasmtime
# Load module, instantiate with empty imports
# Exports: wasm_solve, __wbindgen_add_to_stack_pointer, __wbindgen_export_0
# wasm_solve(ret_ptr, challenge_ptr, challenge_len, prefix_ptr, prefix_len, difficulty_f64)
# Returns: ret_ptr[0:4] = status (i32), ret_ptr[8:16] = answer (f64)
~~~

### Go Solver (from shaohuahuawww/deepseek-pow)

Repo: `https://github.com/shaohuahuawww/deepseek-pow` — has `dsk/pow_solver.go`. Compile with `go build -o pow_solver pow_solver.go`. Takes JSON on stdin, outputs nonce on stdout.

***

## Authentication

- Token is a Bearer JWT stored **in-memory** (not localStorage/cookies) on chat.deepseek.com
- Obtained via Google OAuth flow (accounts.google.com → deepseek.com/sign_in → redirect back)
- The persistent browser profile at `browser-scraper-data/` maintains the session
- To capture token in-browser: monkey-patch `fetch`/`XHR` to intercept `Authorization` header from any outgoing app request (e.g., sending a message)
- Token format: `Bearer km/HzoBys33Sz...` (long base64-like string)

***

## SSE Response Format

The completion endpoint returns `text/event-stream`. Events are `data: {json}` lines:
- `{"v": {"response": {"fragments": [{"type": "RESPONSE", "content": "..."}]}}}` — content chunks
- `{"v": "FINISHED"}` or `{"v": "COMPLETE"}` — stream end
- `data: [DONE]` — final sentinel

***

## What's Left To Do

1. **Write the PoW solver module** for Sable (save hash_impl + solver to `skills/core/` or `connectors/deepseek/`)
2. **Choose backend**: Go binary (compile once, ~50ms) or wasmtime (~100ms) — pure Python too slow
3. **Integrate into DeepSeek connector**: challenge → solve → header → completion request
4. **Handle token refresh**: detect 401/expired token, trigger re-auth flow
5. **End-to-end test**: the PoW was verified against WASM output but the full HTTP flow (solve → send → get response) hasn't been tested yet with our Python solver (previous attempt used wrong algorithm, got INVALID_POW_RESPONSE)

***

## Sources

- `https://github.com/shaohuahuawww/deepseek-pow` — Python + Go implementation
- `https://pypi.org/project/DeepSeekPowSolver/` — PyPI package
- `https://github.com/Kaesra/deepseek-pow-analysis` — WASM analysis
- `https://deepwiki.com/LLM-Red-Team/deepseek-free-api/5.3-proof-of-work-system` — full docs
- WASM binary: `https://fe-static.deepseek.com/chat/static/sha3_wasm_bg.7b9ca65ddd.wasm`
- Worker JS: `https://fe-static.deepseek.com/chat/static/37627.ebf6d8f55d.js`

***

## Files on Disk

| Path | Content |
|:---|:---|
| `/tmp/sha3_wasm.wasm` | Downloaded WASM binary (26KB) |
| `/tmp/sha3_wasm.wat` | Disassembled WAT (10720 lines) |
| `/tmp/sha3_wasm_bg.7b9ca65ddd.wasm` | Same WASM (different name) |
| `/tmp/37627.ebf6d8f55d.js` | Worker JS (PoW orchestration) |
| Browser profile | `/home/sifat/hdd/projects/Sable/browser-scraper-data/` |
