"""Qwen Session & Authentication Manager — handles headers, WAF tokens, and server session creation."""

import os
import time
import uuid
import base64
import json
from pathlib import Path

import httpx
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
            from playwright.async_api import async_playwright
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

        # Step 1: Get STS token
        sts_headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:153.0) Gecko/20100101 Firefox/153.0",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "source": "web",
            "version": "0.2.81",
            "x-request-id": str(uuid.uuid4()),
            "Cookie": cookies,
            "bx-ua": bx_ua or "",
            "bx-umidtoken": bx_umidtoken or "",
            "bx-v": "2.5.37",
            "Origin": "https://chat.qwen.ai",
            "Referer": "https://chat.qwen.ai/",
        }
        sts_payload = {"filename": filename, "filesize": str(filesize), "filetype": "image"}

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                sts_resp = await client.post(
                    "https://chat.qwen.ai/api/v2/files/getstsToken",
                    headers=sts_headers,
                    json=sts_payload,
                )
            if sts_resp.status_code != 200:
                print(f"[ERROR] STS token request failed: HTTP {sts_resp.status_code} — {sts_resp.text[:300]}")
                return None
            sts_data = sts_resp.json()
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

    async def sync_context(self, headers: dict[str, str] | None = None, project_id: str | None = None) -> bool:
        """Sync persona instructions to Qwen via settings/update API (no Playwright DOM)."""
        if headers is None:
            await self.start()

        SETTINGS_URL = "https://chat.qwen.ai/api/v2/users/user/settings/update"

        # Load project config if active
        proj = None
        if project_id:
            try:
                from server.database import get_project
                proj = get_project(project_id)
            except Exception:
                pass

        # Build instruction payload from instruction/ files
        instruction_dir = Path(__file__).resolve().parent.parent / "instruction"
        instructions = ""

        # Project instruction overrides Maria.md if defined
        project_instruction = None
        if proj and proj.get("instruction_text"):
            project_instruction = proj["instruction_text"]
        elif proj and proj.get("instruction_file"):
            instr_path = Path(proj["instruction_file"])
            if instr_path.exists():
                project_instruction = instr_path.read_text(encoding="utf-8")

        if project_instruction and proj and proj.get("persona_enabled", True):
            # Project instruction replaces Maria.md entirely
            instructions += project_instruction + "\n\n"
        else:
            # Default: load Maria.md
            maria_path = instruction_dir / "Maria.md"
            if maria_path.exists():
                instructions += maria_path.read_text(encoding="utf-8") + "\n\n"

        # Output format — skip if project has it disabled
        if not proj or proj.get("output_format_enabled", True):
            of_path = instruction_dir / "output_format.md"
            if of_path.exists():
                instructions += of_path.read_text(encoding="utf-8") + "\n\n"

        # Inject facts to remember if project has them
        if proj and proj.get("facts"):
            instructions += f"\n\n# Facts to Remember (Project: {proj.get('name', 'Unknown')})\n{proj['facts']}\n\n"

        # Inject git details if project has them
        if proj and any(proj.get(k) for k in ("git_repo", "git_username", "git_branch")):
            instructions += "\n\n# Git Repository Details\n"
            if proj.get("git_repo"):
                instructions += f"- Repo: {proj['git_repo']}\n"
            if proj.get("git_username"):
                instructions += f"- Username: {proj['git_username']}\n"
            if proj.get("git_branch"):
                instructions += f"- Branch: {proj['git_branch']}\n"
            instructions += "\n"

        # Collect disabled skills from global file + project config
        _disabled_skills: list[str] = []
        _global_disabled_path = Path(__file__).resolve().parent.parent / "Brain" / "disabled_skills.json"
        if _global_disabled_path.exists():
            try:
                import json as _json
                _gd = _json.loads(_global_disabled_path.read_text(encoding="utf-8"))
                if isinstance(_gd, list):
                    _disabled_skills.extend(_gd)
            except Exception:
                pass
        if proj and proj.get("skills_config"):
            _disabled_skills.extend([k for k, v in proj["skills_config"].items() if not v])

        # Auto-generated skill registry (filtered by disabled skills at discovery)
        from engine.skills import SkillEngine
        from engine.skills.handlers import HANDLER_MAP
        _engine = SkillEngine(
            skills_dir=Path(__file__).resolve().parent.parent / "skills",
            handlers=HANDLER_MAP,
            agent_id="maria",
            disabled=_disabled_skills,
        )
        skills_prompt = _engine.get_registry_prompt()
        instructions += skills_prompt

        # Inject tool schemas (filtered by disabled tools)
        try:
            from engine.tools_loader import get_tools_prompt_section
            _disabled_tools_path = Path(__file__).resolve().parent.parent / "Brain" / "disabled_tools.json"
            _disabled_tools: list[str] = []
            if _disabled_tools_path.exists():
                import json as _json2
                _dt = _json2.loads(_disabled_tools_path.read_text(encoding="utf-8"))
                if isinstance(_dt, list):
                    _disabled_tools = _dt
            tools_section = get_tools_prompt_section(disabled=_disabled_tools)
            if tools_section:
                instructions += chr(10) + chr(10) + tools_section
        except Exception:
            pass

        # Inject connected MCP tools into system prompt
        try:
            from engine.mcp.manager import get_mcp_manager
            mcp_section = get_mcp_manager().get_prompt_section()
            if mcp_section:
                instructions += chr(10) + chr(10) + mcp_section + chr(10) + chr(10)
        except Exception:
            pass



        MAX_CHARS = 40960
        if len(instructions) > MAX_CHARS:
            instructions = instructions[:MAX_CHARS]

        # Use provided headers or fetch fresh from browser
        if headers is None:
            headers = await self.get_fresh_headers()
        headers = dict(headers)  # copy to avoid mutating the cached dict
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