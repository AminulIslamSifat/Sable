"""Live probe: inspect the ChatGPT /f/conversation body shape on acc5."""
from __future__ import annotations
import asyncio, json, sys
from pathlib import Path

PROFILE = Path("/home/sifat/hdd/projects/test/Sable-exp/system/browser-data-acc5")
BIN = "/usr/bin/helium-browser"

INSPECT_JS = r'''
() => {
  if (window.__probeInstalled) return 'already';
  window.__probeInstalled = true;
  window.__probeResult = null;
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
        window.__probeResult = {
          url: u,
          top_keys: Object.keys(b),
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
      } catch (e) { window.__probeResult = { error: String(e) }; }
    }
    return of.apply(this, args);
  };
  return 'installed';
}
'''

SELECTOR_JS = r'''
() => {
  const out = [];
  const cands = [
    "button[data-testid='create-new-chat-button']",
    "a[aria-label='New chat']",
    "button[aria-label='New chat']",
    "a[href='/']",
  ];
  for (const sel of cands) {
    const els = document.querySelectorAll(sel);
    if (els.length) {
      const e = els[0];
      out.push({ sel, count: els.length, tag: e.tagName,
                 text: (e.textContent||'').trim().slice(0,40),
                 aria: e.getAttribute('aria-label'), testid: e.getAttribute('data-testid') });
    } else { out.push({ sel, count: 0 }); }
  }
  return out;
}
'''

async def main():
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE), executable_path=BIN, headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                  "--password-store=basic", "--use-mock-keychain"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        print("[probe] navigating to chatgpt.com ...")
        await page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(4000)

        try:
            tok = await page.evaluate(
                "async () => { try { const r = await fetch('/api/auth/session',"
                "{credentials:'include'}); if (!r.ok) return {ok:false,status:r.status};"
                " const j = await r.json(); return {ok:true, has_token: !!j.accessToken,"
                " email: (j.user && j.user.email) || null}; } catch(e){ return {err:String(e)}; } }")
            print("[probe] auth session:", json.dumps(tok))
        except Exception as e:
            print("[probe] auth check failed:", e)

        try:
            print(f"[probe] url={page.url}")
            print(f"[probe] composer={await page.locator('#prompt-textarea').count()} "
                  f"assistant_msgs={await page.locator(chr(34)+'div[data-message-author-role='+chr(39)+'assistant'+chr(39)+']'+chr(34)).count()}")
        except Exception as e:
            print("[probe] dom check failed:", e)

        try:
            info = await page.evaluate(SELECTOR_JS)
            print("[probe] new-chat candidates:", json.dumps(info, indent=2))
        except Exception as e:
            print("[probe] selector probe failed:", e)

        res = await page.evaluate(INSPECT_JS)
        print("[probe] inspect hook:", res)
        print("[probe] done — hook armed for next real send.")
        await ctx.close()

if __name__ == "__main__":
    asyncio.run(main())
