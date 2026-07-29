"""Qwen Session & Authentication Manager — handles headers, WAF tokens, and server session creation."""

import os
import time
import uuid
import base64
import json
from pathlib import Path

import httpx
from playwright.async_api import async_playwright
from engine.config import COOKIES, BX_UA, BX_UMIDTOKEN, NEW_CHAT_URL, get_model_config


def build_headers(cookies: str | None = None, bx_ua: str | None = None, bx_umidtoken: str | None = None) -> dict[str, str]:
    """Construct HTTP headers with given or fallback cookies and security tokens."""
    return {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:153.0) Gecko/20100101 Firefox/153.0",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "Origin": "https://chat.qwen.ai",
        "Referer": "https://chat.qwen.ai/",
        "X-Accel-Buffering": "no",
        "X-Request-Id": str(uuid.uuid4()),
        "Version": "0.2.78",
        "source": "web",
        "Cookie": cookies or COOKIES,
        "bx-ua": bx_ua or BX_UA,
        "bx-umidtoken": bx_umidtoken or BX_UMIDTOKEN,
        "bx-v": "2.5.37",
    }


class BrowserManager:
    """Manages a single persistent Chromium instance to upload images & sniff headers."""

    def __init__(self, user_data_dir: str | None = None, headless: bool = True):
        if user_data_dir is None:
            from engine.config import BROWSER_DATA_DIR
            user_data_dir = str(BROWSER_DATA_DIR)
        self.user_data_dir = user_data_dir
        self.headless = headless
        self.playwright = None
        self.context = None
        self.page = None

    @property
    def browser_headless(self) -> bool:
        return self.headless

    def _check_profile_lock(self) -> bool:
        """Check if the profile is locked by another Chromium instance."""
        lock_file = Path(self.user_data_dir) / "SingletonLock"
        if lock_file.exists():
            print("[WARN] Profile SingletonLock detected — another browser instance may be running.")
            print("[WARN] Removing stale lock file...")
            try:
                lock_file.unlink()
            except OSError as e:
                print(f"[ERROR] Could not remove lock: {e}")
                return False
        return True

    async def start(self):
        """Lazy-starts the browser context and page if not already running."""
        if not self.playwright:
            self._check_profile_lock()
            print(f"[DEBUG] Launching persistent browser context (headless={self.headless})...")
            self.playwright = await async_playwright().start()
            self.context = await self.playwright.chromium.launch_persistent_context(
                user_data_dir=self.user_data_dir,
                headless=self.headless,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                    "--disable-gpu",
                ],
            )
            self.page = await self.context.new_page()
            await self.page.goto("https://chat.qwen.ai", wait_until="domcontentloaded", timeout=15000)

            # Wait for auth hydration (avatar/user element or timeout)
            try:
                await self.page.wait_for_selector(
                    "button[class*='avatar'], img[class*='avatar'], [class*='user-info']",
                    timeout=8000,
                )
            except Exception:
                print("[WARN] Auth selector not found, falling back to fixed wait...")
                await self.page.wait_for_timeout(4000)

            # Validate auth tokens loaded
            has_token = await self.page.evaluate("() => !!localStorage.getItem('token')")
            if not has_token:
                print("[WARN] ⚠️  No JWT token in localStorage after load!")
                print(f"[WARN] Profile may be stale/corrupted. Try: rm -rf {self.user_data_dir} && re-login via browser_opener.py")
            else:
                print("[DEBUG] ✅ Auth token hydrated successfully.")

            await self.page.add_script_tag(url="https://gosspublic.alicdn.com/aliyun-oss-sdk-6.18.1.min.js")
            await self.page.wait_for_timeout(1000)

    async def restart(self, headless: bool | None = None) -> None:
        """Close and relaunch the browser with an optional new headless flag."""
        if headless is not None:
            self.headless = headless
        await self.close()
        await self.start()

    async def get_fresh_headers(self) -> dict[str, str]:
        """Sniff fresh WAF tokens and cookies using the running browser tab context."""
        await self.start()
        if not self.page or not self.context:
            raise RuntimeError("Browser session is not available")

        captured: dict[str, str] = {}

        def on_request(req) -> None:
            if "api/v2" in req.url:
                h = dict(req.headers)
                if "bx-ua" in h and "bx-umidtoken" in h:
                    captured["bx-ua"] = h["bx-ua"]
                    captured["bx-umidtoken"] = h["bx-umidtoken"]

        self.page.on("request", on_request)
        try:
            probe_urls = [
                "https://chat.qwen.ai/api/v2/users/status",
                "https://chat.qwen.ai/api/v2/chats?page_number=1&page_size=1",
            ]
            status = None
            for probe_url in probe_urls:
                if captured.get("bx-ua") and captured.get("bx-umidtoken"):
                    break
                status = await self.page.evaluate(
                    """async (url) => {
                        try {
                            const res = await fetch(url, { credentials: 'include' });
                            const body = await res.text();
                            return { ok: res.ok, status: res.status, body: body.slice(0, 300) };
                        } catch (err) {
                            return { ok: false, status: 0, body: String((err && err.message) || err) };
                        }
                    }""", probe_url
                )
                await self.page.wait_for_timeout(1500)
        finally:
            self.page.remove_listener("request", on_request)

        cookies = await self.context.cookies()
        cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies]) if cookies else None

        missing = [key for key in ("bx-ua", "bx-umidtoken") if not captured.get(key)]
        if missing:
            status_info = str(status)[:500]
            print(
                "[WARN] Header capture incomplete: missing " + ", ".join(missing) +
                "; cookies=" + ("yes" if cookie_str else "no") +
                "; status=" + status_info +
                "; falling back to configured session tokens"
            )

        fresh = "fresh" if not missing else "fallback"
        print(f"[DEBUG] Using {fresh} WAF headers (bx-ua={'yes' if captured.get('bx-ua') else 'no'}, bx-umidtoken={'yes' if captured.get('bx-umidtoken') else 'no'})")

        return build_headers(
            cookies=cookie_str,
            bx_ua=captured.get("bx-ua"),
            bx_umidtoken=captured.get("bx-umidtoken")
        )

    async def extract_deepseek_token(self) -> str:
        """Read DeepSeek bearer token from the shared persistent browser profile."""
        await self.start()
        if not self.context:
            raise RuntimeError("Browser session is not available")

        page = await self.context.new_page()
        try:
            await page.goto("https://chat.deepseek.com", wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(2000)  # let JS hydrate localStorage
            raw = await page.evaluate("() => localStorage.getItem('userToken')")
        finally:
            await page.close()

        if not raw:
            raise RuntimeError("No DeepSeek userToken found in browser profile. Log in to chat.deepseek.com first.")

        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                token = parsed.get("value", raw)
            else:
                token = raw
        except (json.JSONDecodeError, AttributeError):
            token = raw.strip('"')

        token = str(token).strip()
        if not token:
            raise RuntimeError("DeepSeek userToken was empty after parsing.")
        return token

    async def upload_image(self, image_path: str) -> dict | None:
        """Upload an image via Aliyun OSS JS SDK using the running browser context."""
        await self.start()
        if not os.path.exists(image_path):
            print(f"[ERROR] File not found: {image_path}")
            return None

        filesize = os.path.getsize(image_path)
        filename = os.path.basename(image_path)
        ext = filename.split(".")[-1].lower()
        mime_type = "image/png" if ext == "png" else ("image/jpeg" if ext in ("jpg", "jpeg") else "image/webp")

        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("utf-8")

        print(f"[DEBUG] Uploading '{filename}' ({filesize} bytes) to Aliyun OSS...")

        # Navigate to Qwen chat to ensure fresh session cookies + re-inject OSS SDK
        try:
            current_url = self.page.url
            if "chat.qwen.ai" not in current_url:
                await self.page.goto("https://chat.qwen.ai", wait_until="domcontentloaded", timeout=15000)
            else:
                await self.page.reload(wait_until="domcontentloaded", timeout=15000)
            await self.page.wait_for_timeout(2000)
            await self.page.add_script_tag(url="https://gosspublic.alicdn.com/aliyun-oss-sdk-6.18.1.min.js")
            await self.page.wait_for_timeout(1000)
            print(f"[DEBUG] Browser refreshed at {self.page.url} for fresh session cookies")
        except Exception as e:
            print(f"[WARN] Browser refresh failed (continuing anyway): {e}")

        try:
            js_script = """async ({ b64, filesize, filename, mimeType }) => {
                let tokenRes;
                try {
                    tokenRes = await fetch("https://chat.qwen.ai/api/v2/files/getstsToken", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ filename, filesize: String(filesize), filetype: "image" })
                    });
                } catch (fetchErr) {
                    return { error: "STS fetch failed: " + fetchErr.message };
                }
                if (!tokenRes.ok) {
                    const text = await tokenRes.text().catch(() => "(no body)");
                    return { error: "STS HTTP " + tokenRes.status + ": " + text.slice(0, 300) };
                }
                const stsData = await tokenRes.json();
                if (!stsData.success) return { error: "STS rejected: " + JSON.stringify(stsData).slice(0, 300) };
                const sts = stsData.data;

                const byteCharacters = atob(b64);
                const byteNumbers = new Array(byteCharacters.length);
                for (let i = 0; i < byteCharacters.length; i++) {
                    byteNumbers[i] = byteCharacters.charCodeAt(i);
                }
                const blob = new Blob([new Uint8Array(byteNumbers)], { type: mimeType });

                const client = new OSS({
                    region: sts.region,
                    accessKeyId: sts.access_key_id,
                    accessKeySecret: sts.access_key_secret,
                    stsToken: sts.security_token,
                    bucket: sts.bucketname,
                    endpoint: sts.endpoint,
                    secure: true
                });

                const result = await client.put(sts.file_path, blob);
                return { status: result.res.status, sts: sts };
            }"""

            res = await self.page.evaluate(js_script, {"b64": img_b64, "filesize": filesize, "filename": filename, "mimeType": mime_type})

            if res and res.get("status") in (200, 204) and "sts" in res:
                sts = res["sts"]
                now_ms = int(time.time() * 1000)
                file_id = sts["file_id"]
                file_url = sts["file_url"]

                file_obj = {
                    "type": "image",
                    "file": {
                        "created_at": now_ms,
                        "data": {},
                        "filename": filename,
                        "hash": None,
                        "id": file_id,
                        "user_id": sts["file_path"].split("/")[0],
                        "meta": {"name": filename, "size": filesize, "content_type": mime_type},
                        "update_at": now_ms,
                        "lastModified": now_ms,
                        "name": filename,
                        "webkitRelativePath": "",
                        "size": filesize,
                        "type": mime_type
                    },
                    "id": file_id,
                    "url": file_url,
                    "name": filename,
                    "collection_name": "",
                    "progress": 0,
                    "status": "uploaded",
                    "greenNet": "success",
                    "size": filesize,
                    "error": "",
                    "itemId": str(uuid.uuid4()),
                    "file_type": mime_type,
                    "showType": "image",
                    "file_class": "vision",
                    "uploadTaskId": str(uuid.uuid4())
                }
                print(f"[DEBUG] Image uploaded successfully! File ID: {file_id}")
                return file_obj
            else:
                print(f"[ERROR] Image upload failed: {res}")
        except Exception as e:
            print(f"[ERROR] upload_image exception: {e}")

        return None

    async def sync_context(self) -> bool:
        """Sync persona instructions to Qwen via settings/update API (no Playwright DOM)."""
        await self.start()

        SETTINGS_URL = "https://chat.qwen.ai/api/v2/users/user/settings/update"

        # Build instruction payload from instruction/ files
        instruction_dir = Path(__file__).resolve().parent.parent / "instruction"
        instructions = ""

        maria_path = instruction_dir / "Maria.md"
        if maria_path.exists():
            instructions += maria_path.read_text(encoding="utf-8") + "\n\n"

        for fname in ["output_format.md", "skills.md"]:
            fpath = instruction_dir / fname
            if fpath.exists():
                instructions += fpath.read_text(encoding="utf-8") + "\n\n"

        PROJECT_ROOT = Path(__file__).resolve().parent.parent
        OUTPUT_ROOT = PROJECT_ROOT / "output"
        ASSETS_DIR = OUTPUT_ROOT / "assets"
        instructions += (
            f"\n\n***\n\n# SYSTEM DIRECTORIES\n"
            f"PROJECT_ROOT={PROJECT_ROOT}\n"
            f"OUTPUT_ROOT={OUTPUT_ROOT}\n"
            f"ASSETS_DIR={ASSETS_DIR}\n"
            f"All <OUTPUT_ROOT> tags in your instructions should be replaced with {OUTPUT_ROOT}\n"
            f"All <PROJECT_ROOT> tags in your instructions should be replaced with {PROJECT_ROOT}\n"
        )

        MAX_CHARS = 40960
        if len(instructions) > MAX_CHARS:
            instructions = instructions[:MAX_CHARS]

        # Get fresh auth headers from browser session
        headers = await self.get_fresh_headers()
        headers.update({
            "Content-Type": "application/json",
            "Version": "0.2.80",
            "source": "web",
            "Origin": "https://chat.qwen.ai",
            "Referer": "https://chat.qwen.ai/settings/personalization",
            "X-Request-Id": str(uuid.uuid4()),
        })

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                # Step 1: Disable default Qwen tools that conflict with Sable skills
                tools_payload = {
                    "tools_enabled": {
                        "web_extractor": False,
                        "web_search_image": False,
                        "web_search": False,
                        "image_gen_tool": False,
                        "code_interpreter": False,
                        "history_retriever": False,
                        "image_edit_tool": False,
                        "bio": False,
                        "image_zoom_in_tool": False,
                    }
                }
                r1 = await client.post(SETTINGS_URL, json=tools_payload, headers=headers)
                d1 = r1.json()
                if not d1.get("success"):
                    raise Exception(f"Disable tools failed: {d1}")
                print("[DEBUG] Qwen default tools disabled")

                # Step 2: Update personalization instruction
                instr_payload = {
                    "personalization": {
                        "name": "Sifat",
                        "description": "",
                        "style": "Default",
                        "instruction": instructions,
                    }
                }
                headers["X-Request-Id"] = str(uuid.uuid4())
                r2 = await client.post(SETTINGS_URL, json=instr_payload, headers=headers)
                d2 = r2.json()
                if not d2.get("success"):
                    raise Exception(f"Update instruction failed: {d2}")
                print(f"[DEBUG] Context synced successfully! ({len(instructions)} chars)")
                return True
        except Exception as e:
            print(f"[ERROR] sync_context failed: {e}")
            return False

    async def close(self):
        """Cleanly closes context, saving browser profile state."""
        # FIX: context.close() and playwright.stop() are coroutines in the async
        # API — the old sync `def close` never awaited them, so the browser
        # process was never actually torn down (silent leak + RuntimeWarning).
        if self.context:
            await self.context.close()
            self.context = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None
        self.page = None


async def create_new_chat(headers: dict[str, str], model: str | None = None) -> str | None:
    """Create a new chat session on the server and return the server-generated chat_id.

    `model`, if given, selects which entry from config.MODELS this chat is
    created for (falls back to the default MODEL). Keeping this in sync with
    whatever model build_body() uses matters — the server associates the
    chat session with a model at creation time.
    """
    model_id = get_model_config(model)["id"]
    body = {
        "chatId": "",
        "models": [model_id],
        "project_id": "",
        "timestamp": int(time.time() * 1000),
        "chat_type": "t2t",
        "chat_mode": "normal",
    }
    # FIX: was using the blocking `httpx.post`, which stalls the event loop for
    # the duration of the request if called from async code alongside
    # BrowserManager. Switched to AsyncClient.
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.post(NEW_CHAT_URL, headers=headers, json=body)
        if res.status_code == 200:
            data = res.json()
            if data.get("success"):
                chat_id = data.get("data", {}).get("id")
                print(f"[DEBUG] Server created chat session ID: {chat_id}")
                return chat_id
            print(f"[ERROR] Server refused chat creation: {data}")
        else:
            print(f"[ERROR] HTTP {res.status_code} on chats/new: {res.text[:300]}")
    except Exception as e:
        print(f"[ERROR] create_new_chat failed: {e}")
    return None