"""
Test: JS fetch interceptor for DeepSeek SSE capture.
Attaches to running Chrome on port 9333, injects fetch monkey-patch
(via both add_init_script AND immediate evaluate), sends a test message,
and verifies SSE chunks are captured.
"""
import asyncio
import json
import time
from playwright.async_api import async_playwright

CDP_PORT = 9333
OUTPUT_FILE = "/tmp/deepseek_sse_capture.log"
INPUT_SELECTOR = "textarea[name='search']"
SEND_BTN_SELECTOR = "div[role='button']:has(path[d^='M8.3125 0.981587'])"

FETCH_INTERCEPTOR_JS = """
(() => {
    const TARGET = '/api/v0/chat/completion';
    if (window.__ghost_fetch_patched) return;
    window.__ghost_fetch_patched = true;
    const originalFetch = window.fetch;
    let chunkIndex = 0;

    window.fetch = async function(...args) {
        const url = typeof args[0] === 'string' ? args[0] : args[0]?.url || '';
        const response = await originalFetch.apply(this, args);

        if (!url.includes(TARGET)) return response;

        const clone = response.clone();
        const reader = clone.body.getReader();
        const decoder = new TextDecoder();
        const reqId = 'sse_' + Date.now() + '_' + (++chunkIndex);

        (async () => {
            try {
                while (true) {
                    const { done, value } = await reader.read();
                    if (done) {
                        window.__ds_on_sse_done(reqId);
                        break;
                    }
                    const text = decoder.decode(value, { stream: true });
                    window.__ds_on_sse_chunk(text, reqId);
                }
            } catch (e) {
                console.error('[GhostChat] SSE capture error:', e);
                window.__ds_on_sse_done(reqId);
            }
        })();

        return response;
    };
})();
"""


class SSECapture:
    def __init__(self):
        self.chunks = []
        self.done_event = asyncio.Event()
        self.log = open(OUTPUT_FILE, "w", encoding="utf-8")

    def write(self, msg):
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        self.log.write(line + "\n")
        self.log.flush()

    def on_chunk(self, chunk, req_id):
        self.chunks.append(chunk)
        preview = repr(chunk[:200])
        self.write(f"📦 CHUNK #{len(self.chunks)} ({len(chunk)}B) [{req_id}]: {preview}")

    def on_done(self, req_id):
        total = sum(len(c) for c in self.chunks)
        self.write(f"✅ STREAM DONE: {len(self.chunks)} chunks, {total} bytes")
        full = "".join(self.chunks)
        self.write(f"\n{'='*80}\nFULL SSE BODY:\n{full}\n{'='*80}")
        self.done_event.set()

    def close(self):
        self.log.close()


async def main():
    cap = SSECapture()
    pw = await async_playwright().start()

    try:
        browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()

        # Expose callbacks BEFORE injecting JS
        await page.expose_function("__ds_on_sse_chunk", lambda chunk, req_id: cap.on_chunk(chunk, req_id))
        await page.expose_function("__ds_on_sse_done", lambda req_id: cap.on_done(req_id))

        # Inject via BOTH methods: init_script (for future navigations) AND evaluate (for current page)
        await page.add_init_script(FETCH_INTERCEPTOR_JS)
        await page.evaluate(FETCH_INTERCEPTOR_JS)

        cap.write("✅ Fetch interceptor injected (init_script + evaluate)")
        cap.write(f"   URL: {page.url}")

        if "deepseek.com" not in page.url:
            cap.write("⚠️  Not on deepseek.com, navigating...")
            await page.goto("https://chat.deepseek.com/")
            await asyncio.sleep(5)
            # Re-inject after navigation (add_init_script should handle it, but be safe)
            await page.evaluate(FETCH_INTERCEPTOR_JS)
            cap.write("   Re-injected after navigation")

        # Send test message using correct DeepSeek selectors
        cap.write("\n🔄 Sending test message...")
        try:
            field = page.locator(INPUT_SELECTOR).first
            await field.wait_for(state="visible", timeout=10000)
            await field.click()
            await field.fill("Say exactly: NETWORK_TEST_V4")
            cap.write("   ✓ Message typed")
            await asyncio.sleep(0.5)

            send_btn = page.locator(SEND_BTN_SELECTOR).first
            if await send_btn.count() > 0 and await send_btn.is_visible():
                await send_btn.click()
                cap.write("   ✓ Send clicked")
            else:
                await page.keyboard.press("Enter")
                cap.write("   ✓ Enter pressed")
        except Exception as e:
            cap.write(f"   ⚠️ Could not send: {e}")

        cap.write("\n⏳ Waiting for SSE stream (120s)...")
        try:
            await asyncio.wait_for(cap.done_event.wait(), timeout=120)
            cap.write("\n🎉 Capture complete!")

            # Parse and summarize
            full = "".join(cap.chunks)
            data_lines = [l.strip() for l in full.split("\n") if l.strip().startswith("data:")]
            cap.write(f"\n📊 Summary: {len(data_lines)} data lines from {len(cap.chunks)} chunks")

            content_parts = []
            reasoning_parts = []
            for line in data_lines:
                payload = line[5:].strip()
                if payload == "[DONE]":
                    continue
                try:
                    obj = json.loads(payload)
                    delta = obj.get("choices", [{}])[0].get("delta", {})
                    c = delta.get("content", "")
                    r = delta.get("reasoning_content", "")
                    if c: content_parts.append(c)
                    if r: reasoning_parts.append(r)
                except:
                    pass

            if content_parts:
                cap.write(f"   Content preview: {''.join(content_parts)[:500]}")
            if reasoning_parts:
                cap.write(f"   Reasoning preview: {''.join(reasoning_parts)[:500]}")
            if not content_parts and not reasoning_parts:
                cap.write("   ⚠️ No parseable deltas found — raw first 500 chars:")
                cap.write(f"   {full[:500]}")

        except asyncio.TimeoutError:
            cap.write(f"\n⏰ Timeout. Got {len(cap.chunks)} chunks")
            if cap.chunks:
                full = "".join(cap.chunks)
                cap.write(f"   Partial data ({len(full)}B): {full[:500]}")

    except Exception as e:
        cap.write(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        cap.close()
        await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
