"""DEFINITIVE: capture REAL UI send headers vs GHOST fetch headers on the same
page/account, then diff. Answers why ghost gets banned and scraper doesn't."""
import asyncio, json, sys
sys.path.insert(0, '.')
from connectors.deepseek.client import DeepSeekClient
from connectors.deepseek import ghost as G

ACCT = "browser-data-acc5"


async def main():
    s = await G.GhostSession.for_account(ACCT)
    page = s._page
    await asyncio.sleep(3)
    print(f"[page] {page.url}", flush=True)

    raw = await page.evaluate("() => localStorage.getItem('userToken')")
    token = DeepSeekClient._parse_localstorage_token(raw)
    print(f"[token] {token[:20]}...", flush=True)

    c = DeepSeekClient(account=ACCT)
    c._token = token; c._rotate_tokens = [token]; c._rotate_idx = 0; c._account = ACCT
    headers = c._auth_headers_for(token)
    ch = await c._get_challenge(headers=headers)
    nonce = await asyncio.get_running_loop().run_in_executor(None, c._solve_pow, ch)
    pow_hdr = c._build_pow_header(ch, nonce)
    sess = await c._create_session(headers=headers)
    print(f"[session] {sess}", flush=True)

    cap = {"ui": [], "ghost": []}
    mode = ["idle"]
    cdp = await s._ctx.new_cdp_session(page)
    await cdp.send("Network.enable")

    def on_req(p):
        r = p.get("request", {})
        if "/api/v0/chat/completion" in r.get("url", "") and r.get("method") == "POST":
            cap[mode[0]].append({k.lower(): v for k, v in r.get("headers", {}).items()})

    cdp.on("Network.requestWillBeSent", on_req)

    # ---------- REAL UI SEND ----------
    mode[0] = "ui"
    sel = None
    for cand in ("textarea[name='search']", "#chat-input", "[contenteditable='true']"):
        try:
            if await page.locator(cand).count():
                sel = cand; break
        except Exception:
            pass
    print(f"[ui] composer={sel}", flush=True)
    await page.locator(sel).first.click()
    await page.keyboard.type("ui-probe", delay=20)
    await asyncio.sleep(0.3)
    await page.keyboard.press("Enter")
    await asyncio.sleep(9)
    mode[0] = "idle"
    print(f"[ui] captured {len(cap['ui'])}", flush=True)

    # ---------- GHOST SEND ----------
    mode[0] = "ghost"
    body = {
        "chat_session_id": sess, "parent_message_id": None,
        "model_type": "default", "prompt": "ghost-probe",
        "ref_file_ids": [], "thinking_enabled": False,
        "search_enabled": False, "action": None, "preempt": False,
    }
    print(f"[ghost] injecting {len(s._identity)} identity headers", flush=True)
    try:
        resp = await s.open_stream(token, body, pow_hdr, idle_secs=30)
        async with resp as r:
            async for _ in r.aiter_lines():
                pass
    except Exception as e:
        print(f"[ghost] err {e}", flush=True)
    mode[0] = "idle"
    print(f"[ghost] captured {len(cap['ghost'])}", flush=True)
    try:
        await cdp.detach()
    except Exception:
        pass

    ui = cap["ui"][0] if cap["ui"] else {}
    gh = cap["ghost"][0] if cap["ghost"] else {}

    print("\n" + "=" * 70, flush=True)
    print("HEADER DIFF:  UI(real/scraper path)   vs   GHOST(fetch path)", flush=True)
    print("=" * 70, flush=True)
    for k in sorted(set(ui) | set(gh)):
        u = ui.get(k); g = gh.get(k)
        if u == g:
            tag = "SAME "
        elif u and not g:
            tag = "UI>GH"
        elif g and not u:
            tag = "GH>UI"
        else:
            tag = "DIFF "
        print(f"{tag}  {k}", flush=True)
        if tag != "SAME ":
            print(f"         UI   : {str(u)[:88]}", flush=True)
            print(f"         GHOST: {str(g)[:88]}", flush=True)


asyncio.run(main())
