"""Live check: what does the REAL ChatGPT /f/conversation body contain?

Uses the production ghost path (Helium + ProfileHub) for browser-data-acc5,
installs an inspection hook, sends one benign prompt, dumps the body shape.
"""
import asyncio, json, sys
sys.path.insert(0, "/home/sifat/hdd/projects/test/Sable-exp")

INSPECT_JS = r'''
() => {
  if (window.__inspectInstalled) return 'already';
  window.__inspectInstalled = true;
  window.__inspectResult = null;
  const of = window.fetch;
  window.fetch = async function(...args) {
    const u = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
    const opts = args[1] || {};
    const isSend = u.includes('/backend-api/f/conversation')
                   && !u.includes('prepare') && !u.includes('resume')
                   && (opts.method || 'GET') === 'POST';
    if (isSend) {
      try {
        const b = JSON.parse(opts.body || '{}');
        window.__inspectResult = {
          url: u,
          model: b.model || b.model_slug || null,
          has_tools: Array.isArray(b.tools),
          tools_count: Array.isArray(b.tools) ? b.tools.length : 0,
          tool_names: Array.isArray(b.tools)
            ? b.tools.map(t => (t.function && t.function.name) || t.name || '?') : [],
          has_tool_choice: 'tool_choice' in b,
          conversation_id: b.conversation_id || null,
          parent_message_id: b.parent_message_id || null,
          messages_len: Array.isArray(b.messages) ? b.messages.length : 0,
          msg_roles: Array.isArray(b.messages) ? b.messages.map(m => m.author && m.author.role) : [],
          has_attachments: 'attachments' in b,
          all_keys: Object.keys(b),
        };
      } catch (e) { window.__inspectResult = { error: String(e) }; }
    }
    return of.apply(this, args);
  };
  return 'installed';
}
'''

SELECTOR_JS = r'''
() => {
  const out = [];
  for (const sel of [
    "button[data-testid='create-new-chat-button']",
    "a[aria-label='New chat']",
    "button[aria-label='New chat']",
    "a[href='/']",
  ]) {
    const els = document.querySelectorAll(sel);
    out.push(els.length
      ? { sel, count: els.length, tag: els[0].tagName, testid: els[0].getAttribute('data-testid'), aria: els[0].getAttribute('aria-label') }
      : { sel, count: 0 });
  }
  return out;
}
'''

async def main():
    from connectors.chatgpt.ghost import ChatGPTGhostSession
    sess = await ChatGPTGhostSession.for_account("browser-data-acc5")
    print("[probe] session up; page:", sess._page.url)

    ok = await sess.has_token(wait_secs=8)
    print("[probe] has_token:", ok)

    # Selector probe (needs logged-in DOM)
    try:
        sel = await sess._page.evaluate(SELECTOR_JS)
        print("[probe] new-chat candidates:", json.dumps(sel, indent=2))
    except Exception as e:
        print("[probe] selector probe failed:", e)

    # Is the current page a blank chat?
    try:
        n = await sess._page.locator("div[data-message-author-role]").count()
        print(f"[probe] messages on page: {n}")
    except Exception as e:
        print("[probe] msg count failed:", e)

    # Arm inspection (coexists with the ghost's own hook — it wraps fetch again)
    res = await sess._page.evaluate(INSPECT_JS)
    print("[probe] inspect hook:", res)

    # Send one benign prompt through the ghost
    print("[probe] sending benign prompt ...")
    resp = await sess.open_stream("reply with the single word: pong", idle_secs=30)
    got = []
    async with resp as r:
        async for line in r.aiter_lines():
            got.append(line)
            if len(got) >= 5:
                break
    print("[probe] first SSE lines:", got[:3])

    # Read the captured body shape
    await sess._page.wait_for_timeout(500)
    body = await sess._page.evaluate("() => window.__inspectResult")
    print("[probe] BODY SHAPE:", json.dumps(body, indent=2))

    await ChatGPTGhostSession.close_all()
    print("[probe] done")

if __name__ == "__main__":
    asyncio.run(main())
