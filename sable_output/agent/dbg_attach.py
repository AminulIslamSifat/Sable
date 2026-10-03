import asyncio, json, sys
sys.path.insert(0, "/home/sifat/hdd/projects/test/Sable-exp")
from connectors.chatgpt.ghost import ChatGPTGhostSession

READ = r'''
() => JSON.stringify({
  nmsg: window.__lastBody && window.__lastBody.messages ? window.__lastBody.messages.length : -1,
  m0meta: window.__lastBody && window.__lastBody.messages && window.__lastBody.messages[0] ? (window.__lastBody.messages[0].metadata || null) : null,
  m0content: window.__lastBody && window.__lastBody.messages && window.__lastBody.messages[0] ? (window.__lastBody.messages[0].content || null) : null,
})
'''

async def main():
    sess = await ChatGPTGhostSession.for_account("browser-data-acc5")
    print("[dbg] up, token:", await sess.has_token(wait_secs=6))
    await sess.prepare_new_chat()
    await sess._page.evaluate(r'''
    () => {
      if (window.__dbgHooked) return 'already';
      window.__dbgHooked = true;
      window.__lastBody = null;
      const of = window.fetch;
      window.fetch = async function(...a){
        const u = typeof a[0]==='string'?a[0]:(a[0]&&a[0].url)||'';
        const o = a[1]||{};
        if (u.includes('/f/conversation') && !u.includes('prepare') && !u.includes('resume') && (o.method||'GET')==='POST'){
          try { window.__lastBody = JSON.parse(o.body||'{}'); } catch(e){ window.__lastBody={err:String(e)}; }
        }
        return of.apply(this,a);
      };
      return 'hooked';
    }
    ''')
    with open("/tmp/dbg_attach.txt","w") as f:
        f.write("SECRET=MANGO\n")
    print("[dbg] uploading ...")
    info = await sess.upload_file("/tmp/dbg_attach.txt")
    print("[dbg] upload info:", info)
    pre = await sess._page.evaluate(r'''
    () => JSON.stringify({bodyHasName: document.body.innerText.includes('dbg_attach.txt')})
    ''')
    print("[dbg] pre-send:", pre)
    resp = await sess.open_stream("What is the SECRET value in the attached file?", idle_secs=25)
    n=0
    async with resp as r:
        async for _ in r.aiter_lines():
            n+=1
            if n>=25: break
    await sess._page.wait_for_timeout(500)
    body = await sess._page.evaluate(READ)
    print("[dbg] submitted meta:", body)
    await ChatGPTGhostSession.close_all()

asyncio.run(main())
