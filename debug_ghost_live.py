"""Test: DeepSeek ghost with LIVE page-native signing (no cached identity).

Strips the cached x-hif-* injection, calls /chat/completion via the ghost's
in-page fetch, and captures the ACTUAL outgoing headers via CDP. Verifies
whether DeepSeek's own SPA wrapper mints fresh x-hif-dliq / x-hif-leim.
"""
import asyncio, json, sys
sys.path.insert(0, '.')

from connectors.deepseek.client import DeepSeekClient
from connectors.deepseek import ghost as G

ACCT = "browser-data-acc5"


async def main():
    # Bring up the ghost FIRST, then read a LIVE token from its own
    # localStorage (the stored token may be stale/dead).
    s = await G.GhostSession.for_account(ACCT)
    print(f"[ghost] page_url={s._page.url}", flush=True)
    await asyncio.sleep(3)

    raw = await s._page.evaluate("() => localStorage.getItem('userToken')")
    if not raw:
        print("[FATAL] no userToken in acc5 localStorage — profile not logged in", flush=True)
        return
    token = DeepSeekClient._parse_localstorage_token(raw)
    print(f"[token] live from localStorage: {token[:24]}...", flush=True)

    c = DeepSeekClient(account=ACCT)
    c._token = token
    c._rotate_tokens = [token]
    c._rotate_idx = 0
    c._account = ACCT
    headers = c._auth_headers_for(token)
    print(f"[client hdrs] {sorted(headers.keys())}", flush=True)

    challenge = await c._get_challenge(headers=headers)
    loop = asyncio.get_running_loop()
    nonce = await loop.run_in_executor(None, c._solve_pow, challenge)
    pow_hdr = c._build_pow_header(challenge, nonce)
    print(f"[pow] nonce={nonce}", flush=True)

    sess_id = await c._create_session(headers=headers)
    print(f"[session] {sess_id}", flush=True)

    body = {
        "chat_session_id": sess_id, "parent_message_id": None,
        "model_type": "default", "prompt": "Say exactly: live-ok",
        "ref_file_ids": [], "thinking_enabled": False,
        "search_enabled": False, "action": None, "preempt": False,
    }

    probe = await s._page.evaluate(
        "() => ({title: document.title, url: location.href, "
        "hasComposer: !!document.querySelector(\"textarea[name='search'], "
        "#chat-input, [contenteditable='true']\"), "
        "fetchPatched: !!(window.fetch.toString().includes('originalFetch') "
        "|| window.fetch.toString().includes('native code')===false), "
        "fetchSrc: window.fetch.toString().slice(0,80)})")
    print(f"[probe] {probe}", flush=True)

    captured = []
    cdp = await s._ctx.new_cdp_session(s._page)
    await cdp.send("Network.enable")

    def on_req(p):
        r = p.get("request", {})
        if "/api/v0/chat/completion" in r.get("url", "") and r.get("method") == "POST":
            captured.append({k.lower(): v for k, v in r.get("headers", {}).items()})

    cdp.on("Network.requestWillBeSent", on_req)

    saved = dict(s._identity)
    s._identity = {}
    print(f"[mode] identity stripped (was {len(saved)} hdrs)", flush=True)

    data_lines = []
    err = None
    try:
        resp = await s.open_stream(token, body, pow_hdr, idle_secs=30)
        async with resp as r:
            print(f"[stream] status={r.status_code}", flush=True)
            async for line in r.aiter_lines():
                if line.startswith("data: ") and line[6:] != "[DONE]":
                    data_lines.append(line)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    finally:
        s._identity = saved
        try:
            await cdp.detach()
        except Exception:
            pass

    if err:
        print(f"[error] {err}", flush=True)

    print(f"\n[SSE] {len(data_lines)} data lines", flush=True)
    for l in data_lines[:8]:
        print("   ", l[:170], flush=True)

    print(f"\n[CAPTURED] {len(captured)} completion POST(s)", flush=True)
    for i, h in enumerate(captured):
        print(f"--- request {i} ---", flush=True)
        for k in sorted(h):
            v = str(h[k])
            if k in ("authorization", "x-ds-pow-response"):
                print(f"  {k}: {v[:36]}...", flush=True)
            elif k.startswith("x-hif") or k in ("x-device-id",) or k.startswith("x-client"):
                print(f"  {k}: {v[:60]}", flush=True)
            else:
                print(f"  {k}: {v[:48]}", flush=True)


asyncio.run(main())
