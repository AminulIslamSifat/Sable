import asyncio, sys
sys.path.insert(0, "/home/sifat/hdd/projects/test/Sable-exp")
async def main():
    from connectors.chatgpt.ghost import ChatGPTGhostSession
    s = await ChatGPTGhostSession.for_account("browser-data-acc6")
    print("[h6] up:", s._page.url, "token:", await s.has_token(wait_secs=8))
    await asyncio.sleep(25)
asyncio.run(main())
