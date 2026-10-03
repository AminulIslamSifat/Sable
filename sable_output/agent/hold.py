import asyncio, sys
sys.path.insert(0, "/home/sifat/hdd/projects/test/Sable-exp")
async def main():
    from connectors.chatgpt.ghost import ChatGPTGhostSession
    await ChatGPTGhostSession.for_account("browser-data-acc5")
    print("[h] held")
    await asyncio.sleep(30)
asyncio.run(main())
