"""Install catch-all AFTER the ghost's own hook, every navigation."""
import asyncio, json, sys
sys.path.insert(0, "/home/sifat/hdd/projects/test/Sable-exp")
from connectors.chatgpt.ghost import ChatGPTGhostSession

CATCHALL_JS = r'''
() => {
  if (window.__catchInstalled2) return 'already';
  window.__catchInstalled2 = true;
  window.__catch = [];
  window.__convoBody = null;
  const of = window.fetch;
  window.fetch = async function(...args) {
    const u = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
    const opts = args[1] || {};
    const method = opts.method || 'GET';
    if (u.includes('/backend-api/')) {
      window.__catch.push({ u: u.slice(0,180), method });
      if (u.includes('/f/conversation') && !u.includes('prepare')
          && !u.includes('resume') && method === 'POST') {
        try {
          const b = JSON.parse(opts.body || '{}');
          window.__convoBody = {
            url: u, top_keys: Object.keys(b),
            model: b.model || b.model_slug || null,
            has_tools: Array.isArray(b.tools),
            tools_count: Array.isArray(b.tools) ? b.tools.length : 0,
            tool_names: Array.isArray(b.tools) ? b.tools.map(t => (t.function&&t.function.name)||t.name||'?') : [],
            tool_choice: b.tool_choice !== undefined ? b.tool_choice : null,
            conversation_id: b.conversation_id || null,
            parent_message_id: b.parent_message_id || null,
            messages_len: Array.isArray(b.messages) ? b.messages.length : 0,
            msg_roles: Array.isArray(b.messages) ? b.messages.map(m => m.author&&m.author.role) : [],
            has_attachments: 'attachments' in b,
            keys: Object.keys(b),
          };
        } catch(e) { window.__convoBody = { error: String(e) }; }
      }
    }
    return of.apply(this, args);
  };
  return 'installed';
}
'''

_orig_install = ChatGPTGhostSession._install_hijack
async def patched_install(self):
    await _orig_install(self)
    try:
        await self._page.evaluate(CATCHALL_JS)
        print("[probe] catch-all armed post-hijack")
    except Exception as e:
        print("[probe] catch-all arm failed:", e)
ChatGPTGhostSession._install_hijack = patched_install

async def main():
    sess = await ChatGPTGhostSession.for_account("browser-data-acc5")
    print("[probe] up:", sess._page.url, "| has_token:", await sess.has_token(wait_secs=8))
    resp = await sess.open_stream("Say exactly: pong", idle_secs=30)
    n = 0
    async with resp as r:
        async for line in r.aiter_lines():
            n += 1
            if n >= 30: break
    print(f"[probe] consumed {n} SSE lines")
    await sess._page.wait_for_timeout(1000)
    body = await sess._page.evaluate("() => window.__convoBody")
    seen = await sess._page.evaluate("() => window.__catch")
    print("[probe] BODY:", json.dumps(body, indent=2))
    print("[probe] SEEN:", json.dumps(seen, indent=2))
    await ChatGPTGhostSession.close_all()
    print("[probe] done")

asyncio.run(main())
