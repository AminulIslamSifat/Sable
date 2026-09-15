"""Qwen Session & Authentication Manager — handles headers, WAF tokens, and server session creation."""

import asyncio
import os
import time
import uuid
import base64
import json
from datetime import datetime
from pathlib import Path
from typing import Any


from engine.config import COOKIES, BX_UA, BX_UMIDTOKEN, NEW_CHAT_URL, get_model_config


def _get_user_name() -> str:
    """Get display name from config (env-overridable)."""
    from engine.config import USER_NAME
    return USER_NAME


# Persistent Playwright launch counter — survives restarts.
_LAUNCH_COUNTER_PATH = Path(__file__).resolve().parent.parent / "system" / ".playwright_launches"


def _increment_playwright_counter() -> int:
    """Atomically increment and return the Playwright launch counter."""
    try:
        count = int(_LAUNCH_COUNTER_PATH.read_text().strip()) if _LAUNCH_COUNTER_PATH.exists() else 0
    except (ValueError, OSError):
        count = 0
    count += 1
    try:
        _LAUNCH_COUNTER_PATH.write_text(str(count))
    except OSError:
        pass
    return count


def _account_ua(account: str | None) -> tuple[str, str]:
    """(user_agent, sec-ch-ua) for the browser that created `account`.

    Falls back to the Playwright-bundled Chromium when the account is unknown
    or has no saved browser. Never raises — a broken accounts.json must not
    break outbound requests.
    """
    try:
        from engine.platform_paths import get_account_ua_fingerprint
        return get_account_ua_fingerprint(account)
    except Exception:
        from engine.platform_paths import derive_ua_fingerprint
        return derive_ua_fingerprint(None)


def _current_account_ua() -> tuple[str, str]:
    """(user_agent, sec-ch-ua) for the browser that created the active account."""
    try:
        from engine.config import get_active_account
        return _account_ua(get_active_account())
    except Exception:
        return _account_ua(None)


def build_headers(
    cookies: str | None = None,
    bx_ua: str | None = None,
    bx_umidtoken: str | None = None,
    referer: str | None = None,
    account: str | None = None,
) -> dict[str, str]:
    """Construct HTTP headers with given or fallback cookies and security tokens.

    Mirrors the real chat.qwen.ai Chromium web client so the WAF fingerprint
    (UA + sec-ch-ua + bx-ua token) stays internally consistent. The UA and
    sec-ch-ua brands are derived from the Chromium version of the browser that
    created the account (see accounts.json → browser_path), so a Helium-created
    account announces Chromium 153, not a hardcoded guess.

    `referer` should be the full chat URL when known
    (https://chat.qwen.ai/c/<chat_id>), falling back to the site root.
    """
    _tz = datetime.now().astimezone().strftime("%a %b %d %Y %H:%M:%S GMT%z")

    user_agent, sec_ch_ua = _account_ua(account) if account else _current_account_ua()

    return {
        "User-Agent": user_agent,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "Referer": referer or "https://chat.qwen.ai/",
        "Timezone": _tz,
        "X-Accel-Buffering": "no",
        "X-Request-Id": str(uuid.uuid4()),
        "Version": "0.2.91",
        "source": "web",
        "sec-ch-ua": sec_ch_ua,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Linux"',
        # ── Sec-Fetch-* headers ──────────────────────────────────────────
        # curl_cffi's impersonate="chrome" auto-injects these as if the
        # request were a page navigation (Dest: document, Mode: navigate,
        # Site: none). But Qwen's API is called via JS fetch(), which sends
        # completely different values. Alibaba's WAF flags the mismatch
        # instantly — a POST to /completions with Sec-Fetch-Mode: navigate
        # is impossible for a real browser.
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        # ── Strip navigation-only headers ────────────────────────────────
        # curl_cffi also leaks Sec-Fetch-User and Upgrade-Insecure-Requests
        # which only exist on top-level navigations, never on fetch() calls.
        # Setting them to empty string prevents curl_cffi from injecting
        # its defaults. We handle actual removal in the caller.
        "Cookie": cookies or COOKIES,
        "bx-ua": bx_ua or BX_UA,
        "bx-umidtoken": bx_umidtoken or BX_UMIDTOKEN,
        "bx-v": "2.5.37",
    }


# Headers that curl_cffi's impersonate="chrome" auto-injects for page navigations
# but that a real browser NEVER sends on fetch()/XHR API calls. If these leak
# through, Alibaba's WAF sees an impossible combination (JSON POST + navigation
# headers) and flags it as bot traffic.
_NAVIGATION_ONLY_HEADERS = frozenset({
    "sec-fetch-user",
    "upgrade-insecure-requests",
})


def sanitize_fetch_headers(headers: dict[str, str]) -> dict[str, str]:
    """Remove navigation-only headers that curl_cffi leaks into API requests.

    Call this right before passing headers to curl_cffi for any Qwen API call.
    Returns a new dict — doesn't mutate the original.
    """
    return {
        k: v for k, v in headers.items()
        if k.lower() not in _NAVIGATION_ONLY_HEADERS
    }


class BrowserManager:
    """Manages a single persistent Chromium instance to upload images & sniff headers."""

    def __init__(self, user_data_dir: str | None = None, headless: bool = True):
        if user_data_dir is None:
            from engine.config import get_browser_data_dir
            user_data_dir = str(get_browser_data_dir())
        self.user_data_dir = user_data_dir
        self.headless = headless
        self.playwright = None
        self.context = None
        self.page = None

    @property
    def browser_headless(self) -> bool:
        return self.headless

    @property
    def is_running(self) -> bool:
        """True if the persistent browser context is currently up."""
        return self.playwright is not None

    def _check_profile_lock(self) -> bool:
        """Check if the profile is locked by another Chromium instance."""
        lock_file = Path(self.user_data_dir) / "SingletonLock"
        # Use is_symlink() | exists() because SingletonLock is a symlink to
        # "hostname-PID". If the target process is dead, exists() returns False
        # but the dangling symlink still blocks Chromium from launching.
        if lock_file.is_symlink() or lock_file.exists():
            # Check if the PID in the symlink target is actually alive
            stale = True
            if lock_file.is_symlink():
                try:
                    target = lock_file.readlink().name  # e.g. "Archie-24367"
                    pid_str = target.rsplit("-", 1)[-1]
                    pid = int(pid_str)
                    import signal
                    os.kill(pid, 0)  # raises OSError if process doesn't exist
                    stale = False  # process is alive — real lock
                except (ValueError, OSError, ProcessLookupError):
                    stale = True
            
            if stale:
                print("[DEBUG] Removing stale SingletonLock...")
                try:
                    lock_file.unlink()
                except OSError as e:
                    print(f"[ERROR] Could not remove lock: {e}")
                    return False
            else:
                print("[WARN] Profile locked by active Chromium process")
                return False
        return True

    async def start(self):
        """Lazy-starts the browser context and page if not already running."""
        if self.playwright and self.context:
            return
        self._check_profile_lock()
        launch_num = _increment_playwright_counter()
        print(f"[DEBUG] Launching persistent browser context #{launch_num} (headless={self.headless})...")
        from playwright.async_api import async_playwright
        from engine.platform_paths import resolve_browser_for_profile, extra_browser_args
        try:
            self.playwright = await async_playwright().start()
            profile_name = getattr(self, "profile_name", None) or Path(self.user_data_dir).name
            exe_path = resolve_browser_for_profile(profile_name)
            launch_kwargs: dict[str, Any] = dict(
                user_data_dir=self.user_data_dir,
                headless=self.headless,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                    "--disable-gpu",
                ] + extra_browser_args(exe_path),
            )
            if exe_path:
                launch_kwargs["executable_path"] = exe_path
            self.context = await self.playwright.chromium.launch_persistent_context(**launch_kwargs)
            self.page = await self.context.new_page()
            await self.page.goto("https://chat.qwen.ai", wait_until="domcontentloaded", timeout=15000)

            # Poll localStorage directly for auth token (faster than waiting for UI selectors in headless)
            has_token = False
            for _ in range(12):  # up to 6s, checking every 500ms
                has_token = await self.page.evaluate("() => !!localStorage.getItem('token')")
                if has_token:
                    break
                await self.page.wait_for_timeout(500)

            if not has_token:
                print("[WARN] ⚠️  No JWT token in localStorage after 6s!")
                print(f"[WARN] Profile may be stale/corrupted. Try: rm -rf {self.user_data_dir} && re-login via browser_opener.py")
            else:
                print("[DEBUG] ✅ Auth token hydrated successfully.")

            await self.page.add_script_tag(url="https://gosspublic.alicdn.com/aliyun-oss-sdk-6.18.1.min.js")
            await self.page.wait_for_timeout(1000)
        except Exception:
            # Reset state so a subsequent retry doesn't see a half-initialized manager
            await self.close()
            raise

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

        # Wait for critical Alibaba WAF cookies to be set by page JS.
        # acw_tc (WAF session), isg (bot detection token), and tfstk (fingerprint)
        # are generated asynchronously by Alibaba's anti-bot SDK. Without them,
        # httpx requests get captcha-challenged even with perfect headers.
        for _ in range(12):  # up to 6s, checking every 500ms
            _check_cookies = await self.context.cookies()
            _cookie_names = {c["name"] for c in _check_cookies}
            if "acw_tc" in _cookie_names and "isg" in _cookie_names:
                print("[DEBUG] WAF cookies (acw_tc, isg) baked successfully")
                break
            await self.page.wait_for_timeout(500)
        else:
            print("[WARN] WAF cookies (acw_tc/isg) not detected after 6s — captcha likely")

        all_cookies = await self.context.cookies()
        # Only include cookies that a real browser would send to chat.qwen.ai.
        # Playwright's context stores cross-domain cookies (DeepSeek, mmstat, etc.)
        # that a browser would never leak to Qwen. Sending them is a fingerprint red flag.
        _qwen_domains = (".qwen.ai", "chat.qwen.ai", ".alibaba.com")
        filtered_cookies = [
            c for c in all_cookies
            if any(c["domain"] == d or c["domain"].endswith(d) for d in _qwen_domains)
        ]
        cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in filtered_cookies]) if filtered_cookies else None

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

        # Use the live page URL as Referer when we're on a chat page, so the
        # request carries the same https://chat.qwen.ai/c/<id> referer the real
        # client sends.
        referer = None
        try:
            cur = self.page.url if self.page else ""
            if cur.startswith("https://chat.qwen.ai/c/"):
                referer = cur
        except Exception:
            pass

        return build_headers(
            cookies=cookie_str,
            bx_ua=captured.get("bx-ua"),
            bx_umidtoken=captured.get("bx-umidtoken"),
            referer=referer,
        )

    async def get_live_headers(self) -> dict[str, str]:
        """Instantly read current headers from the live browser context.

        No HTTP probes, no waiting. Reads cookies directly from the Playwright
        cookie jar and extracts bx-ua/bx-umidtoken from the Baxia SDK's global
        state in the page JS context. Takes <100ms instead of 3-6s.

        Call this before every request to always send the freshest tokens.
        Requires the browser to already be running (via start() or get_fresh_headers()).
        """
        # Don't try to launch — just reuse whatever is already running.
        # If the browser isn't up yet, fall back to get_fresh_headers().
        if not self.page or not self.context:
            if not self.playwright:
                print("[DEBUG] Browser not running, doing full fresh header setup...")
                return await self.get_fresh_headers()
            raise RuntimeError("Browser session is not available")

        # Read cookies directly — instant, no network needed
        all_cookies = await self.context.cookies()
        _qwen_domains = (".qwen.ai", "chat.qwen.ai", ".alibaba.com")
        filtered_cookies = [
            c for c in all_cookies
            if any(c["domain"] == d or c["domain"].endswith(d) for d in _qwen_domains)
        ]
        cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in filtered_cookies]) if filtered_cookies else None

        # Extract bx-ua and bx-umidtoken from the Baxia SDK's JS globals.
        # The SDK stores its state on window.__baxia__ or similar objects.
        # We try multiple known access patterns.
        bx_ua: str | None = None
        bx_umidtoken: str | None = None
        try:
            baxia_state = await self.page.evaluate("""() => {
                const result = { bx_ua: null, bx_umidtoken: null };
                try {
                    // Pattern 1: Baxia global object
                    if (window.__baxia__) {
                        const b = window.__baxia__;
                        if (typeof b.getUA === 'function') result.bx_ua = b.getUA();
                        if (b.umidToken) result.bx_umidtoken = b.umidToken;
                        if (b.token) result.bx_umidtoken = b.token;
                    }
                    // Pattern 2: AWSC namespace
                    if (!result.bx_ua && window.AWSC) {
                        const a = window.AWSC;
                        if (typeof a.getUA === 'function') result.bx_ua = a.getUA();
                    }
                    // Pattern 3: __umid_getinfo
                    if (!result.bx_umidtoken && typeof window.__umid_getinfo === 'function') {
                        const info = window.__umid_getinfo();
                        if (info && info.token) result.bx_umidtoken = info.token;
                    }
                    // Pattern 4: scan for umid token in meta tags
                    if (!result.bx_umidtoken) {
                        const meta = document.querySelector('meta[name="umid-token"]');
                        if (meta) result.bx_umidtoken = meta.getAttribute('content');
                    }
                } catch(e) {}
                return result;
            }""")
            bx_ua = baxia_state.get("bx_ua") if isinstance(baxia_state, dict) else None
            bx_umidtoken = baxia_state.get("bx_umidtoken") if isinstance(baxia_state, dict) else None
        except Exception:
            pass

        # Fallback: if JS extraction failed, do ONE fast fetch probe
        if not bx_ua or not bx_umidtoken:
            captured: dict[str, str] = {}
            def on_req(req) -> None:
                if "api/v2" in req.url:
                    h = dict(req.headers)
                    if "bx-ua" in h:
                        captured["bx-ua"] = h["bx-ua"]
                    if "bx-umidtoken" in h:
                        captured["bx-umidtoken"] = h["bx-umidtoken"]
            self.page.on("request", on_req)
            try:
                await self.page.evaluate("""async () => {
                    try { await fetch('/api/v2/users/status', { credentials: 'include' }); } catch(e) {}
                }""")
                await self.page.wait_for_timeout(800)
            finally:
                self.page.remove_listener("request", on_req)
            bx_ua = bx_ua or captured.get("bx-ua")
            bx_umidtoken = bx_umidtoken or captured.get("bx-umidtoken")

        referer = None
        try:
            cur = self.page.url if self.page else ""
            if cur.startswith("https://chat.qwen.ai/c/"):
                referer = cur
        except Exception:
            pass

        return build_headers(
            cookies=cookie_str,
            bx_ua=bx_ua,
            bx_umidtoken=bx_umidtoken,
            referer=referer,
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

    async def upload_image(self, image_path: str, cookies: str | None = None, bx_ua: str | None = None, bx_umidtoken: str | None = None) -> dict | None:
        """Upload an image via direct HTTP (STS token + Aliyun OSS PUT). No Playwright JS needed."""
        if not os.path.exists(image_path):
            print(f"[ERROR] File not found: {image_path}")
            return None

        filesize = os.path.getsize(image_path)
        filename = os.path.basename(image_path)
        ext = filename.split(".")[-1].lower()
        mime_type = "image/png" if ext == "png" else ("image/jpeg" if ext in ("jpg", "jpeg") else "image/webp")

        # Fallback to config constants if caller didn't provide credentials
        cookies = cookies or COOKIES
        bx_ua = bx_ua or BX_UA
        bx_umidtoken = bx_umidtoken or BX_UMIDTOKEN

        print(f"[DEBUG] Uploading '{filename}' ({filesize} bytes) via direct HTTP...")

        # Fingerprint derived from the browser that created the active account.
        _ua, _sec_ch_ua = _current_account_ua()

        # Step 1: Get STS token
        sts_headers = {
            "User-Agent": _ua,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Timezone": datetime.now().astimezone().strftime("%a %b %d %Y %H:%M:%S GMT%z"),
            "source": "web",
            "version": "0.2.91",
            "x-request-id": str(uuid.uuid4()),
            "Cookie": cookies,
            "bx-ua": bx_ua or "",
            "bx-umidtoken": bx_umidtoken or "",
            "bx-v": "2.5.37",
            "sec-ch-ua": _sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Linux"',
            "Referer": "https://chat.qwen.ai/",
        }
        sts_payload = {"filename": filename, "filesize": str(filesize), "filetype": "image"}

        try:
            from curl_cffi.requests import AsyncSession as _CffiSession
            async with _CffiSession(impersonate="chrome", timeout=15) as client:
                sts_resp = await client.post(
                    "https://chat.qwen.ai/api/v2/files/getstsToken",
                    headers=sts_headers,
                    json=sts_payload,
                )
            if sts_resp.status_code != 200:
                print(f"[ERROR] STS token request failed: HTTP {sts_resp.status_code} — {sts_resp.content.decode(errors='replace')[:300]}")
                return None
            sts_data = json.loads(sts_resp.content)
            if not sts_data.get("success"):
                print(f"[ERROR] STS token rejected: {json.dumps(sts_data)[:300]}")
                return None
            sts = sts_data["data"]
        except Exception as e:
            print(f"[ERROR] STS token request exception: {e}")
            return None

        # Step 2: Upload to Aliyun OSS
        try:
            import oss2
            auth = oss2.StsAuth(sts["access_key_id"], sts["access_key_secret"], sts["security_token"])
            bucket = oss2.Bucket(auth, f"https://{sts['endpoint']}", sts["bucketname"])

            with open(image_path, "rb") as f:
                put_result = bucket.put_object(sts["file_path"], f)

            if put_result.status not in (200, 204):
                print(f"[ERROR] OSS PUT failed with status {put_result.status}")
                return None
        except Exception as e:
            print(f"[ERROR] OSS upload failed: {e}")
            return None

        # Step 3: Build file object
        now_ms = int(time.time() * 1000)
        file_id = sts["file_id"]
        file_url = sts.get("file_url", "")

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

    async def sync_context(self, headers: dict[str, str] | None = None, project_id: str | None = None, custom_instructions: str | None = None, layout_mode: str | None = None) -> bool:
        """Sync persona instructions to Qwen via settings/update API (no Playwright DOM).

        Args:
            custom_instructions: If provided, use this string directly instead of
                building instructions via build_instructions(). Used by subagents
                to push their own system prompt into Qwen's personalization slot.
            layout_mode: "chat" strips tools/skills/MCP except web search + chat_title.
        """
        # Try disk-cached tokens first — this is a pure API call, no browser needed.
        # Only fall back to launching Playwright if no cached tokens exist.
        if headers is None:
            from engine.config import get_qwen_tokens_for_account
            cached = get_qwen_tokens_for_account()
            if cached and cached.get("cookies"):
                headers = build_headers(
                    cookies=cached["cookies"],
                    bx_ua=cached.get("bx_ua"),
                    bx_umidtoken=cached.get("bx_umidtoken"),
                )
                print("[DEBUG] sync_context: using disk-cached tokens")
            else:
                await self.start()

        SETTINGS_URL = "https://chat.qwen.ai/api/v2/users/user/settings/update"

        if custom_instructions is not None:
            instructions = custom_instructions
        else:
            # Build instructions using shared builder.
            # provider="qwen" injects <action> tag format instructions (Qwen's native wrapper).
            from connectors.common.instruction_builder import build_instructions
            instructions = build_instructions(project_id=project_id, provider="qwen", layout_mode=layout_mode)

        MAX_CHARS = 40960
        if len(instructions) > MAX_CHARS:
            instructions = instructions[:MAX_CHARS]

        # Use provided headers or fetch fresh from browser
        if headers is None:
            headers = await self.get_fresh_headers()
        headers = dict(headers)  # copy to avoid mutating the cached dict

        headers.update({
            "Content-Type": "application/json",
            "Version": "0.2.91",
            "source": "web",
            "Referer": "https://chat.qwen.ai/settings/personalization",
            "X-Request-Id": str(uuid.uuid4()),
        })

        try:
            from curl_cffi.requests import AsyncSession as _CffiSession

            # Step 1: Disable default Qwen tools that conflict with Sable skills.
            # Each step uses its own session because curl_cffi auto-stores Set-Cookie
            # responses in its jar and appends them to subsequent requests, creating
            # duplicate cookies that Qwen's WAF rejects as tampered.
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
            async with _CffiSession(impersonate="chrome", timeout=15) as s1:
                r1 = await s1.post(SETTINGS_URL, json=tools_payload, headers=headers)
                d1 = json.loads(r1.content)
                if r1.status_code == 401 or d1.get("data", {}).get("code") == "Unauthorized":
                    print(f"[WARN] sync_context: {r1.status_code} Unauthorized — response: {str(d1)[:300]}")
                    return False
                if not d1.get("success"):
                    raise Exception(f"Disable tools failed: {d1}")
                print("[DEBUG] Qwen default tools disabled")

            # Step 2: Update personalization instruction (separate session)
            instr_payload = {
                "personalization": {
                    "name": _get_user_name(),
                    "description": "",
                    "style": "Default",
                    "instruction": instructions,
                }
            }
            headers["X-Request-Id"] = str(uuid.uuid4())
            async with _CffiSession(impersonate="chrome", timeout=15) as s2:
                r2 = await s2.post(SETTINGS_URL, json=instr_payload, headers=headers)
                d2 = json.loads(r2.content)
                if not d2.get("success"):
                    raise Exception(f"Update instruction failed: {d2}")
                try:
                    with open("test/ins.md", "w") as file:
                        file.write(instructions)
                except:
                    print("writing to ins.md failed")
                print(f"[DEBUG] Context synced successfully! ({len(instructions)} chars)")
                return True
        except Exception as e:
            print(f"[ERROR] sync_context failed: {e}")
            return False

    async def close(self):
        """Cleanly closes context, saving browser profile state."""
        # Stop keepalive first
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
    """Create a new upstream Qwen session and return its session ID.

    This returns the **upstream** Qwen session ID, NOT a local Sable chat_id.
    Callers should store it via ``set_upstream_session_id(local_chat_id, result)``.

    `model`, if given, selects which entry from config.MODELS this session is
    created for (falls back to the default MODEL).
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
    # curl_cffi with impersonate="chrome" — same TLS fix as stream_chat.
    # httpx uses Python/OpenSSL which Alibaba's WAF fingerprints and blocks.
    try:
        from curl_cffi.requests import AsyncSession
        async with AsyncSession(impersonate="chrome", timeout=15) as client:
            res = await client.post(NEW_CHAT_URL, headers=headers, json=body)
        if res.status_code == 200:
            import json as _json
            raw = res.content.decode(errors="replace")
            # Guard against WAF returning HTML captcha page with HTTP 200
            if raw.lstrip().startswith("<"):
                print("[ERROR] WAF captcha challenge on chats/new — tokens likely stale")
                return None
            data = _json.loads(raw)
            if data.get("success"):
                chat_id = data.get("data", {}).get("id")
                print(f"[DEBUG] Server created chat session ID: {chat_id}")
                return chat_id
            print(f"[ERROR] Server refused chat creation: {data}")
        else:
            print(f"[ERROR] HTTP {res.status_code} on chats/new: {res.content.decode(errors='replace')[:300]}")
    except Exception as e:
        print(f"[ERROR] create_new_chat failed: {e}")
    return None