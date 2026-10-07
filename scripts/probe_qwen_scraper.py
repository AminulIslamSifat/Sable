"""Probe: run the Qwen scraper engine live and log what the SPA actually streams.

Usage:  python scripts/probe_qwen_scraper.py [--headless]

Launches the qwen_scraper engine on the ACTIVE qwen_scraper account, sends one
short message, and dumps:
  * every SSE chunk the fetch/XHR patch captured (raw)
  * the parsed (content, reasoning) deltas
  * the stop-button selector match state
  * the DOM response text
This tells us exactly where capture is sluggish/broken.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors.qwen_scraper.client import SPEC, QwenScraperEngine  # noqa: E402
from engine.config import _SYSTEM, get_provider_active_account  # noqa: E402


async def main() -> None:
    headless = "--headless" in sys.argv
    acct = get_provider_active_account("qwen_scraper")
    print(f"=== probe qwen_scraper | account={acct} | headless={headless} ===")

    eng = QwenScraperEngine(
        SPEC,
        viewer=not headless,
        show_thoughts=True,
        user_data_dir=str(_SYSTEM / acct),
        profile_name=acct,
        kill_existing=True,
    )
    await eng.launch()
    print("launched, port:", eng.port)
    print("page url:", eng.page.url)

    # Is the stop button present/visible at rest?
    stop_sel = SPEC.sel("stop")
    try:
        cnt = await eng.page.locator(stop_sel).count()
        vis = await eng.page.locator(stop_sel).first.is_visible() if cnt else False
    except Exception as e:
        cnt, vis = -1, f"err {e}"
    print(f"stop selector {stop_sel!r}: count={cnt} visible_at_rest={vis}")

    # Instrument the chunk callback to count raw bytes.
    raw_chunks: list[str] = []
    orig_on_chunk = eng._on_sse_chunk

    def spy(chunk: str, req_id: str) -> None:
        raw_chunks.append(chunk)
        orig_on_chunk(chunk, req_id)

    eng._on_sse_chunk = spy  # type: ignore[assignment]

    # Fresh chat + send.
    await eng.new_chat()
    await eng._ensure_fetch_patched()
    eng._reset_sse_capture()

    import time
    t0 = time.time()
    deltas: list[tuple[float, str, str]] = []

    # live_display is called SYNCHRONOUSLY by the engine — must not be async.
    def live(_t: str) -> None:
        deltas.append((time.time() - t0, "live", _t[-60:]))

    sent = await eng.send_msg("Count from 1 to 20, one number per line", raw=True)
    print(f"send_msg -> {sent} at t={time.time()-t0:.2f}s")

    text = await eng.get_response(live_display=live)
    dt = time.time() - t0

    print(f"\n=== RESULT (t={dt:.2f}s) ===")
    print("raw chunk count:", len(raw_chunks))
    print("raw bytes:", sum(len(c) for c in raw_chunks))
    print("live callback fires:", len(deltas))
    print("final text:", repr(text[:400]))

    print("\n--- first 6 raw chunks (truncated) ---")
    for c in raw_chunks[:6]:
        print(repr(c[:300]))

    print("\n--- parsed deltas (first 10) ---")
    shown = 0
    for c in raw_chunks:
        ans, think = eng.parse_sse_delta(c)
        if ans or think:
            print("  ans=%r think=%r" % (ans[:80], think[:80]))
            shown += 1
            if shown >= 10:
                break

    await eng.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
