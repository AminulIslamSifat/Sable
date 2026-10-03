"""End-to-end: ChatGPT connector stream_chat with a file attached."""
import asyncio, sys
sys.path.insert(0, "/home/sifat/hdd/projects/test/Sable-exp")

async def main():
    from connectors.chatgpt.client import ChatGPTClient
    cli = ChatGPTClient()
    print("[test] account:", cli.account)
    with open("/tmp/attach_e2e.txt", "w") as f:
        f.write("The secret word is PINEAPPLE. Remember it.\n")
    msg = "What is the secret word in the attached file? Answer in one word."
    got_ans = []
    errs = []
    async for ev in cli.stream_chat(
        msg,
        chat_id=None,
        files=["/tmp/attach_e2e.txt"],
        inject_instructions=False,
    ):
        t = ev.get("type")
        if t == "status":
            print("[test] status:", ev.get("message"))
        elif t == "answer":
            got_ans.append(ev.get("text", ""))
        elif t == "error":
            errs.append(ev.get("message"))
        elif t == "done":
            print("[test] done, chat_id:", ev.get("chat_id"))
    ans = "".join(got_ans).strip()
    print("[test] ERROR:", errs)
    print("[test] ANSWER:", repr(ans[:300]))
    print("[test] SECRET FOUND:", "PINEAPPLE" in ans.upper())

asyncio.run(main())
