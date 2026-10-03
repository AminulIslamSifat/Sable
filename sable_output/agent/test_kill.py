import asyncio, sys
sys.path.insert(0, "/home/sifat/hdd/projects/test/Sable-exp")
async def main():
    from connectors.chatgpt.ghost import ChatGPTGhostSession
    sess = await ChatGPTGhostSession.for_account("browser-data-acc5")
    print("[k] ghost up:", sess._page.url)
    print("[k] token:", await sess.has_token(wait_secs=6))
    await ChatGPTGhostSession.close_all()
    print("[k] done")
asyncio.run(main())
