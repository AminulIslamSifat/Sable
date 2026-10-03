"""Persistent ghost browser for acc5 — start ONCE, reuse across probes.

Speaks line-JSON over stdin/stdout so we never respawn a browser:
  {"cmd":"send","text":"..."}      -> sends, returns SSE summary + body
  {"cmd":"capture_upload"}          -> arms upload-endpoint logger
  {"cmd":"dump"}                    -> returns last captured body/upload
  {"cmd":"quit"}
"""
import asyncio, json, sys
sys.path.insert(0, "/home/sifat/hdd/projects/test/Sable-exp")
from connectors.chatgpt.ghost import ChatGPTGhostSession

HOOK_JS = r'''
() => {
  if (window.__hookV2) return 'already';
  window.__hookV2 = true;
  window.__convoBody = null;
  window.__uploadCalls = [];
  window.__fileRefs = [];
  const of = window.fetch;
  window.fetch = async function(...args) {
    const u = typeof args[0]==='string'?args[0]:(args[0]&&args[0].url)||'';
    const opts = args[1]||{};
    const method = opts.method||'GET';
    if (u.includes('/backend-api/')) {
      // capture file-upload endpoints (any of the known patterns)
      if (/files|upload|attachments/i.test(u)) {
        let bodyPeek = null;
        try {
          if (opts.body && typeof opts.body === 'string' && opts.body.length < 2000) {
            bodyPeek = opts.body.slice(0,800);
          }
        } catch(e){}
        const entry = { u: u.slice(0,200), method, bodyPeek };
        window.__uploadCalls.push(entry);
      }
      if (u.includes('/f/conversation') && !u.includes('prepare')
          && !u.includes('resume') && method==='POST') {
        try {
          const b = JSON.parse(opts.body||'{}');
          window.__convoBody = {
            url: u,
            keys: Object.keys(b),
            model: b.model||null,
            has_tools: Array.isArray(b.tools),
            conversation_id: b.conversation_id||null,
            parent_message_id: b.parent_message_id||null,
            messages_len: Array.isArray(b.messages)?b.messages.length:0,
            has_attachments: 'attachments' in b,
            attachments: b.attachments||null,
            msg0_keys: (b.messages&&b.messages[0])?Object.keys(b.messages[0]):[],
            msg0_content: (b.messages&&b.messages[0]&&b.messages[0].content)?b.messages[0].content:null,
          };
        } catch(e){ window.__convoBody={error:String(e)}; }
      }
    }
    return of.apply(this,args);
  };
  return 'installed';
}
'''

async def arm(sess):
    try:
        await sess._page.evaluate(HOOK_JS)
    except Exception as e:
        return f"arm-failed: {e}"
    return "armed"

async def main():
    sess = await ChatGPTGhostSession.for_account("browser-data-acc5")
    tok = await sess.has_token(wait_secs=8)
    print(json.dumps({"ready": True, "token": tok, "url": sess._page.url}), flush=True)
    loop = asyncio.get_event_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            break
        try:
            req = json.loads(line)
        except Exception as e:
            print(json.dumps({"error": f"bad json: {e}"}), flush=True); continue
        cmd = req.get("cmd")
        if cmd == "quit":
            break
        elif cmd == "arm":
            print(json.dumps({"arm": await arm(sess)}), flush=True)
        elif cmd == "send":
            await arm(sess)
            resp = await sess.open_stream(req.get("text","hi"), idle_secs=req.get("idle",25))
            n = 0
            async with resp as r:
                async for _ in r.aiter_lines():
                    n += 1
                    if n >= req.get("maxlines",25): break
            await sess._page.wait_for_timeout(600)
            body = await sess._page.evaluate("() => window.__convoBody")
            print(json.dumps({"sse_lines": n, "body": body}), flush=True)
        elif cmd == "dump":
            body = await sess._page.evaluate("() => window.__convoBody")
            ups = await sess._page.evaluate("() => window.__uploadCalls")
            print(json.dumps({"body": body, "uploads": ups}), flush=True)
        else:
            print(json.dumps({"error": f"unknown cmd {cmd}"}), flush=True)
    await ChatGPTGhostSession.close_all()
    print(json.dumps({"bye": True}), flush=True)

asyncio.run(main())
