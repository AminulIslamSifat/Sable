#!/usr/bin/env python3
"""Perchance AI Image Generator CLI — standalone tool for Sable.

Usage:
    python3 image_generator.py generate --prompt "..." [--style ghibli] [--shape square] [--count 1]
    python3 image_generator.py list-styles
    python3 image_generator.py styles-json

Can also be imported as a module:
    from tools.image_generator.image_generator import generate_image, STYLES, SHAPES
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
from pathlib import Path

try:
    import httpx
except ImportError:
    print("Error: httpx not installed. Run: pip install httpx", file=sys.stderr)
    sys.exit(1)

# ─── Constants ───────────────────────────────────────────────────────────────

BASE = "https://image-generation.perchance.org/api"
AD_ACCESS_CODE = "a4c88828629d9d1e2c98fa76fd2b5eccb69ee18af472e567febff4b943e9bbd6"
KEY_CACHE_FILE = Path(__file__).resolve().parent.parent.parent.parent / "system" / ".perchance_key"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "Referer": "https://image-generation.perchance.org/embed",
    "Content-Type": "text/plain;charset=UTF-8",
}

OUTPUT_DIR = Path("/home/sifat/sable_output/assets")


# ─── Key Management ──────────────────────────────────────────────────────────

def _verify_key(key: str) -> bool:
    """Check if a Perchance userKey is still valid."""
    try:
        url = f"{BASE}/checkVerificationStatus?userKey={key}&__cacheBust={random.random()}"
        resp = httpx.get(url, headers=HEADERS, timeout=10)
        data = resp.json()
        return data.get("status") == "verified"
    except Exception:
        return False


def save_key(key: str) -> None:
    """Save a key to cache file (JSON format)."""
    KEY_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {"userKey": key.strip(), "adAccessCode": ""}
    KEY_CACHE_FILE.write_text(json.dumps(data, indent=2))


def _load_cached_key() -> str | None:
    """Load key from cache file (supports JSON or raw hex)."""
    if not KEY_CACHE_FILE.exists():
        return None
    try:
        raw = KEY_CACHE_FILE.read_text().strip()
        if not raw:
            return None
        # Try JSON format first
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data.get("userKey")
        except (json.JSONDecodeError, TypeError):
            pass
        # Fallback: raw hex string
        if len(raw) == 64:
            return raw
    except Exception:
        pass
    return None


def _refresh_key_via_api() -> str | None:
    """Get a fresh Perchance userKey via the verifyUser API endpoint.
    
    No browser needed — verifyUser returns a valid key directly.
    Falls back to browser if API fails.
    """
    print("[perchance][key] Attempting API key refresh via verifyUser...", file=sys.stderr)
    try:
        url = f"{BASE}/verifyUser?thread=0&__cacheBust={random.random()}"
        resp = httpx.get(url, headers=HEADERS, timeout=15)
        data = resp.json()
        key = data.get("userKey", "")
        status = data.get("status", "")
        print(f"[perchance][key] verifyUser response: status={status}, key={key[:16]}..." if key else f"[perchance][key] verifyUser response: status={status}, no key", file=sys.stderr)
        if key and len(key) == 64:
            save_key(key)
            print(f"[perchance][key] Got fresh key via API: {key[:16]}...", file=sys.stderr)
            return key
        print(f"[perchance][key] verifyUser returned invalid key (len={len(key)})", file=sys.stderr)
    except Exception as e:
        print(f"[perchance][key] verifyUser API failed: {e}", file=sys.stderr)
    return None


def _refresh_key_via_browser() -> str | None:
    """Launch stealth Chrome browser to capture a fresh Perchance userKey (fallback)."""
    import shutil
    import subprocess
    import signal

    print("[perchance][browser] === Starting browser key refresh (fallback) ===", file=sys.stderr)

    # Find available Chrome-based browser
    browser_bin = None
    for name in ["helium-browser", "google-chrome", "google-chrome-stable", "chromium"]:
        path = shutil.which(name)
        if path:
            browser_bin = path
            print(f"[perchance][browser] Found browser: {name} -> {path}", file=sys.stderr)
            break
    if not browser_bin:
        for p in ["/opt/helium-browser-bin/chrome", "/usr/bin/google-chrome"]:
            if os.path.isfile(p) and os.access(p, os.X_OK):
                browser_bin = p
                print(f"[perchance][browser] Found browser at hardcoded path: {p}", file=sys.stderr)
                break
    if not browser_bin:
        print("[perchance][browser] ERROR: No Chrome browser found for key refresh", file=sys.stderr)
        return None

    # Find free port
    import socket
    cdp_port = 9222
    for port in range(9222, 9230):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                cdp_port = port
                break
    print(f"[perchance][browser] Selected CDP port: {cdp_port}", file=sys.stderr)

    user_data_dir = f"/tmp/perchance_key_refresh_{os.getpid()}"
    print(f"[perchance][browser] User data dir: {user_data_dir}", file=sys.stderr)

    # Inherit display env so Wayland/X11 works in subprocess
    browser_env = os.environ.copy()
    if not browser_env.get("WAYLAND_DISPLAY"):
        # Auto-detect the active Wayland socket
        import glob as _glob
        uid = os.getuid()
        sockets = sorted(_glob.glob(f"/run/user/{uid}/wayland-[0-9]*"))
        # Filter out .lock and non-socket files
        sockets = [s for s in sockets if os.path.basename(s) != f"wayland-{os.path.basename(s)}.lock"
                   and not s.endswith(".lock") and not s.endswith(".sock")]
        if sockets:
            browser_env["WAYLAND_DISPLAY"] = os.path.basename(sockets[0])
            print(f"[perchance][browser] Auto-detected WAYLAND_DISPLAY: {browser_env['WAYLAND_DISPLAY']}", file=sys.stderr)
        else:
            browser_env["WAYLAND_DISPLAY"] = "wayland-0"
            print("[perchance][browser] No Wayland socket found, defaulting to wayland-0", file=sys.stderr)
    else:
        print(f"[perchance][browser] Using existing WAYLAND_DISPLAY: {browser_env['WAYLAND_DISPLAY']}", file=sys.stderr)

    cmd = [
        browser_bin,
        f"--remote-debugging-port={cdp_port}",
        "--disable-blink-features=AutomationControlled",
        "--no-first-run", "--no-default-browser-check", "--no-sandbox",
        "--ozone-platform=wayland",
        f"--user-data-dir={user_data_dir}",
        "about:blank",
    ]
    print(f"[perchance][browser] Launching: {' '.join(cmd)}", file=sys.stderr)

    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=browser_env
    )
    print(f"[perchance][browser] Browser process started (PID: {proc.pid})", file=sys.stderr)

    # Wait for CDP endpoint to become available before connecting
    import urllib.request
    cdp_ready = False
    for attempt in range(30):  # up to ~6 seconds
        try:
            resp = urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json/version", timeout=1)
            version_info = json.loads(resp.read())
            cdp_ready = True
            print(f"[perchance][browser] CDP ready after {attempt+1} attempts. Browser: {version_info.get('Browser', 'unknown')}", file=sys.stderr)
            break
        except Exception as e:
            if attempt % 5 == 4:
                print(f"[perchance][browser] CDP not ready after {attempt+1} attempts: {e}", file=sys.stderr)
            # Check if browser process died
            poll = proc.poll()
            if poll is not None:
                stderr_out = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                print(f"[perchance][browser] ERROR: Browser process exited with code {poll} before CDP ready!", file=sys.stderr)
                print(f"[perchance][browser] stderr: {stderr_out[:1000]}", file=sys.stderr)
                return None
            time.sleep(0.2)

    if not cdp_ready:
        stderr_out = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        print(f"[perchance][browser] ERROR: Browser failed to start CDP after 30 attempts.", file=sys.stderr)
        print(f"[perchance][browser] stderr: {stderr_out[:1000]}", file=sys.stderr)
        proc.kill()
        return None

    captured_key = None
    try:
        print("[perchance][browser] Entering async key capture...", file=sys.stderr)
        captured_key = asyncio.run(_async_capture_key(cdp_port, user_data_dir))
        print(f"[perchance][browser] Async key capture returned: {'SUCCESS (' + captured_key[:16] + '...)' if captured_key else 'FAILED (None)'}", file=sys.stderr)
    except Exception as e:
        import traceback
        print(f"[perchance][browser] ERROR: Browser key capture exception: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
    finally:
        print(f"[perchance][browser] Cleaning up browser (PID: {proc.pid})...", file=sys.stderr)
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
            print(f"[perchance][browser] Browser terminated gracefully", file=sys.stderr)
        except subprocess.TimeoutExpired:
            print(f"[perchance][browser] Browser did not exit in 5s, force killing", file=sys.stderr)
            proc.kill()
        import shutil as _shutil
        _shutil.rmtree(user_data_dir, ignore_errors=True)
        print(f"[perchance][browser] Cleanup complete", file=sys.stderr)

    if captured_key:
        save_key(captured_key)
        print(f"[perchance][browser] Key refreshed and saved: {captured_key[:16]}...", file=sys.stderr)
    else:
        print("[perchance][browser] WARNING: No key captured, browser session ended without success", file=sys.stderr)
    return captured_key


async def _async_capture_key(cdp_port: int, user_data_dir: str) -> str | None:
    """Async helper: navigate Perchance, trigger gen, capture userKey via CDP.
    
    Strategy: Navigate first to spawn the iframe, then attach CDP Network monitoring
    directly to the image-generation.perchance.org target BEFORE triggering generation.
    Cross-origin iframes are separate CDP targets — parent session can't see their requests.
    """
    from playwright.async_api import async_playwright
    from urllib.parse import urlparse, parse_qs
    import websockets

    print("[perchance][capture] Starting async key capture...", file=sys.stderr)
    await asyncio.sleep(3)
    captured = None

    async with async_playwright() as p:
        print(f"[perchance][capture] Connecting to CDP at port {cdp_port}...", file=sys.stderr)
        try:
            browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
        except Exception as e:
            print(f"[perchance][capture] ERROR: Failed to connect to CDP: {e}", file=sys.stderr)
            return None
        print(f"[perchance][capture] Connected. Contexts: {len(browser.contexts)}", file=sys.stderr)

        if not browser.contexts:
            print("[perchance][capture] ERROR: No browser contexts found", file=sys.stderr)
            return None

        context = browser.contexts[0]
        page = await context.new_page()
        print(f"[perchance][capture] New page created", file=sys.stderr)

        # Step 1: Navigate to main page
        print("[perchance][capture] Navigating to perchance.org/ai-text-to-image-generator...", file=sys.stderr)
        try:
            await page.goto("https://perchance.org/ai-text-to-image-generator", wait_until="domcontentloaded", timeout=60000)
            print(f"[perchance][capture] Page loaded. Title: {await page.title()}", file=sys.stderr)
        except Exception as e:
            print(f"[perchance][capture] ERROR: Navigation failed: {e}", file=sys.stderr)
            await page.close()
            return None

        print("[perchance][capture] Waiting 6s for page to fully render...", file=sys.stderr)
        await asyncio.sleep(6)

        # Step 2: Find textarea frame and trigger generation
        print(f"[perchance][capture] Searching for textarea frame among {len(page.frames)} frames...", file=sys.stderr)
        target_frame = None
        for i, frame in enumerate(page.frames):
            try:
                frame_url = frame.url
                print(f"[perchance][capture]   Frame {i}: {frame_url[:100]}", file=sys.stderr)
                ta = await frame.query_selector('textarea[data-name="description"]')
                if ta:
                    target_frame = frame
                    print(f"[perchance][capture]   ✓ Found textarea in frame {i}", file=sys.stderr)
                    break
            except Exception as e:
                print(f"[perchance][capture]   Frame {i} query failed: {e}", file=sys.stderr)
                continue

        if not target_frame:
            print("[perchance][capture] ERROR: No textarea frame found in any frame", file=sys.stderr)
            await page.close()
            return None

        # Trigger generation — this spawns image-generation.perchance.org iframes
        print("[perchance][capture] Filling textarea and triggering generation...", file=sys.stderr)
        await target_frame.fill('textarea[data-name="description"]', "test", force=True)
        await asyncio.sleep(0.3)
        await target_frame.press('textarea[data-name="description"]', "Enter")
        print("[perchance][capture] Generation triggered, waiting 3s for image-gen iframe...", file=sys.stderr)
        await asyncio.sleep(3)

        # Step 3: Find image-generation.perchance.org iframe (spawns AFTER generation trigger)
        print("[perchance][capture] Scanning CDP targets for image-generation iframe...", file=sys.stderr)
        gen_ws = None
        for attempt in range(20):
            try:
                targets = httpx.get(f"http://127.0.0.1:{cdp_port}/json").json()
            except Exception as e:
                print(f"[perchance][capture] WARNING: Failed to fetch CDP targets (attempt {attempt+1}): {e}", file=sys.stderr)
                await asyncio.sleep(1)
                continue

            print(f"[perchance][capture] Attempt {attempt+1}/20: {len(targets)} CDP targets", file=sys.stderr)
            for t in targets:
                url = t.get("url", "")
                ttype = t.get("type", "")
                if ttype == "iframe" and "image-generation.perchance.org" in url:
                    gen_ws = t.get("webSocketDebuggerUrl")
                    print(f"[perchance][capture] ✓ Found image-gen iframe: {url[:100]}", file=sys.stderr)
                    break
            if gen_ws:
                break
            await asyncio.sleep(1)

        if not gen_ws:
            print("[perchance][capture] ERROR: No image-generation iframe found after 20 attempts", file=sys.stderr)
            print("[perchance][capture] All CDP targets:", file=sys.stderr)
            try:
                targets = httpx.get(f"http://127.0.0.1:{cdp_port}/json").json()
                for t in targets:
                    print(f"  [{t.get('type','?')}] {t.get('url','')[:120]}", file=sys.stderr)
            except Exception as e:
                print(f"[perchance][capture] Could not list targets: {e}", file=sys.stderr)
            await page.close()
            return None

        # Step 4: Attach to image-gen iframe, clear stale key, reload, capture fresh key
        print(f"[perchance][capture] Connecting websocket to image-gen iframe...", file=sys.stderr)
        try:
            async with websockets.connect(gen_ws, max_size=2**20) as ws:
                print("[perchance][capture] Websocket connected, enabling Network + Runtime...", file=sys.stderr)
                await ws.send(json.dumps({"id": 1, "method": "Network.enable"}))
                await ws.recv()
                await ws.send(json.dumps({"id": 2, "method": "Runtime.enable"}))
                await ws.recv()

                # Clear localStorage to force fresh key generation
                print("[perchance][capture] Clearing iframe localStorage to force new key...", file=sys.stderr)
                await ws.send(json.dumps({
                    "id": 3,
                    "method": "Runtime.evaluate",
                    "params": {"expression": "localStorage.clear(); 'cleared'"}
                }))
                clear_resp = json.loads(await ws.recv())
                print(f"[perchance][capture] localStorage.clear() result: {clear_resp.get('result', {}).get('result', {}).get('value', 'unknown')}", file=sys.stderr)

                # Reload the iframe so it generates a fresh key
                print("[perchance][capture] Reloading iframe to get fresh key...", file=sys.stderr)
                await ws.send(json.dumps({"id": 4, "method": "Page.reload"}))
                reload_resp = json.loads(await ws.recv())
                print(f"[perchance][capture] Page.reload result: {reload_resp}", file=sys.stderr)

                # Wait for reload to complete and new requests to fire
                await asyncio.sleep(5)
                print("[perchance][capture] Waiting for fresh key in network traffic...", file=sys.stderr)

                deadline = asyncio.get_event_loop().time() + 60
                msg_count = 0
                network_requests = 0
                while asyncio.get_event_loop().time() < deadline:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=3.0)
                        msg_count += 1
                        data = json.loads(msg)
                        method = data.get("method", "")

                        if method == "Network.requestWillBeSent":
                            network_requests += 1
                            req = data.get("params", {}).get("request", {})
                            req_url = req.get("url", "")

                            # Log interesting requests
                            if "perchance" in req_url or "userKey" in req_url:
                                print(f"[perchance][capture] Network request #{network_requests}: {req_url[:150]}", file=sys.stderr)

                            # Check URL params
                            if "userKey" in req_url:
                                qs = parse_qs(urlparse(req_url).query)
                                if "userKey" in qs:
                                    key = qs["userKey"][0]
                                    if len(key) == 64:
                                        captured = key
                                        print(f"[perchance][capture] ✓✓✓ CAPTURED userKey from URL: {key[:16]}...", file=sys.stderr)
                                        break
                                    else:
                                        print(f"[perchance][capture] userKey wrong length ({len(key)}): {key[:16]}...", file=sys.stderr)

                            # Check POST body
                            post_data = req.get("postData", "")
                            if post_data and "userKey" in post_data:
                                try:
                                    body = json.loads(post_data)
                                    key = body.get("userKey", "")
                                    if len(key) == 64:
                                        captured = key
                                        print(f"[perchance][capture] ✓✓✓ CAPTURED userKey from POST: {key[:16]}...", file=sys.stderr)
                                        break
                                    else:
                                        print(f"[perchance][capture] POST userKey wrong length ({len(key)})", file=sys.stderr)
                                except (json.JSONDecodeError, TypeError) as e:
                                    print(f"[perchance][capture] POST body parse error: {e}", file=sys.stderr)

                        elif method and "Network" in method:
                            # Log other Network events at lower frequency
                            if msg_count % 20 == 0:
                                print(f"[perchance][capture] Network event: {method} (msg #{msg_count})", file=sys.stderr)

                    except asyncio.TimeoutError:
                        elapsed = asyncio.get_event_loop().time() - (deadline - 60)
                        print(f"[perchance][capture] Waiting... {elapsed:.0f}s elapsed, {msg_count} msgs, {network_requests} network requests", file=sys.stderr)
                        continue
                    except Exception as e:
                        print(f"[perchance][capture] ERROR: WS recv error: {e}", file=sys.stderr)
                        break

                print(f"[perchance][capture] Loop ended. Total msgs: {msg_count}, network requests: {network_requests}, captured: {bool(captured)}", file=sys.stderr)
        except Exception as e:
            print(f"[perchance][capture] ERROR: Websocket connection failed: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc(file=sys.stderr)

        if not captured:
            print("[perchance][capture] FAILED: No userKey found in network traffic after 60s", file=sys.stderr)

        print("[perchance][capture] Closing page...", file=sys.stderr)
        await page.close()
        print("[perchance][capture] Page closed, disconnecting browser...", file=sys.stderr)

    print(f"[perchance][capture] Async capture finished. Result: {'SUCCESS' if captured else 'FAILED'}", file=sys.stderr)
    return captured


def get_valid_key(override_key: str | None = None) -> str:
    """Get a valid Perchance userKey.
    
    Priority: override_key > PERCHANCE_KEY env > cached file > browser refresh
    Raises RuntimeError if no valid key found after all attempts.
    """
    candidates = []
    if override_key:
        candidates.append(("override", override_key.strip()))
    env_key = os.environ.get("PERCHANCE_KEY", "")
    if env_key:
        candidates.append(("env", env_key.strip()))
    cached = _load_cached_key()
    if cached:
        candidates.append(("cache", cached))

    print(f"[perchance][key] Checking {len(candidates)} candidate(s): {[s for s,_ in candidates]}", file=sys.stderr)
    for source, key in candidates:
        if len(key) != 64:
            print(f"[perchance][key] Skipping {source} key: wrong length ({len(key)})", file=sys.stderr)
            continue
        print(f"[perchance][key] Verifying {source} key: {key[:16]}...", file=sys.stderr)
        valid = _verify_key(key)
        print(f"[perchance][key] {source} key verification: {'VALID ✅' if valid else 'INVALID ❌'}", file=sys.stderr)
        if valid:
            save_key(key)
            return key

    # All candidates failed — try API refresh first (fast, no browser)
    print("[perchance][key] All cached keys invalid, attempting API refresh...", file=sys.stderr)
    new_key = _refresh_key_via_api()
    if new_key and len(new_key) == 64:
        return new_key

    # API failed — fall back to browser
    print("[perchance][key] API refresh failed, falling back to browser...", file=sys.stderr)
    new_key = _refresh_key_via_browser()
    if new_key and len(new_key) == 64:
        return new_key

    raise RuntimeError(
        "No valid Perchance userKey found and browser refresh failed.\n"
        "Set one via: --key YOUR_KEY, PERCHANCE_KEY env var, or ensure a Chrome browser is available."
    )

SHAPES = {
    "square": "768x768",
    "portrait": "512x768",
    "landscape": "768x512",
}

STYLES = {
    # Anime / Manga
    "painted_anime": "painterly anime artwork, world-class masterpiece, fine details, breathtaking artwork, painterly art style, high quality, 8k, very detailed, high resolution, exquisite composition and lighting",
    "anime": "anime art style, high quality anime illustration, detailed anime artwork",
    "ghibli": "Studio Ghibli art style, Hayao Miyazaki inspired, soft watercolor anime, whimsical, beautiful scenery",
    "your_name": "Your Name anime art style, Makoto Shinkai inspired, breathtaking sky, lens flare, vivid colors",
    "wlop": "inspired by wlop art style, digital painting, ethereal lighting, semi-realistic anime",
    "atey_ghailan": "art in the style of atey ghailan, painterly anime style, pixiv masterpiece",
    "kantoku": "in the style of kantoku, soft pastel anime, delicate linework, beautiful eyes",
    "redjuice": "in the style of redjuice, mechanical anime art, detailed sci-fi design",
    # Painting / Traditional
    "oil_painting": "oil painting, classical art style, rich textures, dramatic lighting, museum quality",
    "watercolor": "watercolor painting, soft washes, delicate colors, artistic, paper texture",
    "acrylic": "acrylic painting, bold brushstrokes, vibrant colors, textured canvas",
    "impressionist": "impressionist painting style, Monet inspired, soft brushstrokes, light and color",
    "ukiyo_e": "ukiyo-e art style, traditional Japanese woodblock print, flat colors, bold outlines",
    "art_nouveau": "Art Nouveau style, Mucha inspired, ornamental, flowing organic lines, elegant",
    "renaissance": "Renaissance painting style, classical composition, chiaroscuro, oil on canvas",
    # Digital / Modern
    "digital_art": "digital art, concept art, highly detailed, professional illustration",
    "concept_art": "concept art, matte painting, cinematic, epic scale, professional game art",
    "pixel_art": "pixel art style, retro game aesthetic, 16-bit, crisp pixels, limited palette",
    "comic_book": "comic book art style, bold ink lines, halftone dots, dynamic poses, Marvel/DC style",
    "manga": "manga art style, black and white, screentones, dynamic action lines",
    "cartoon": "cartoon style, clean lines, bright colors, expressive characters, Disney/Pixar inspired",
    # Photography / Realistic
    "photorealistic": "photorealistic, hyperrealistic, 8k uhd, dslr photo, sharp focus, professional photography",
    "cinematic": "cinematic shot, movie still, dramatic lighting, anamorphic lens, film grain, color graded",
    "portrait": "professional portrait photography, studio lighting, shallow depth of field, bokeh background",
    # Stylized / Artistic
    "cyberpunk": "cyberpunk art style, neon lights, dark futuristic city, rain-slicked streets, holographic",
    "steampunk": "steampunk art style, brass gears, Victorian era technology, steam-powered, ornate machinery",
    "vaporwave": "vaporwave aesthetic, retro 80s/90s, neon pink and purple, glitch art, nostalgic",
    "fantasy": "epic fantasy art, magical, dragons and castles, D&D inspired, dramatic lighting",
    "sci_fi": "science fiction art, space opera, advanced technology, alien worlds, cosmic",
    "horror": "dark horror art, eerie atmosphere, Lovecraftian, unsettling, shadowy",
    "surreal": "surrealist art style, Dali inspired, dreamlike, impossible geometry, melting forms",
    "pop_art": "pop art style, Warhol inspired, bold colors, commercial art aesthetic, screenprint",
    "low_poly": "low poly 3D art style, geometric, faceted surfaces, minimalist, clean",
    "isometric": "isometric art style, 3/4 view, clean vector, game asset aesthetic",
    "mtg_card": "Magic the Gathering card art, incredible fantasy artwork, epic composition",
    "50s_enamel": "50s enamel sign art, vintage advertisement, retro Americana, bold typography",
    "no_style": "",
}

DEFAULT_NEGATIVE = "low-quality, deformed, blurry, bad art, extra fingers, mutated hands"


# ─── Core API ────────────────────────────────────────────────────────────────

def _generate_single(full_prompt: str, neg: str, resolution: str, seed: int, user_key: str | None = None) -> dict:
    """Generate a single image via Perchance API. Returns raw result dict."""
    if user_key is None:
        user_key = get_valid_key()

    body = {
        "prompt": full_prompt,
        "negativePrompt": neg,
        "resolution": resolution,
        "guidanceScale": 7,
        "numInferenceSteps": 30,
        "seed": seed,
        "numImages": 1,
    }

    request_id = str(random.random())
    cache_bust = str(random.random())
    url = (
        f"{BASE}/generate"
        f"?userKey={user_key}"
        f"&requestId={request_id}"
        f"&adAccessCode={AD_ACCESS_CODE}"
        f"&__cacheBust={cache_bust}"
    )

    try:
        resp = httpx.post(url, content=json.dumps(body), headers=HEADERS, timeout=360)
    except Exception as e:
        return {"ok": False, "error": f"Request failed: {e}"}

    if resp.status_code != 200:
        return {"ok": False, "error": f"API returned {resp.status_code}: {resp.text[:300]}"}

    data = resp.json()

    # Auto-refresh key if invalid
    if data.get("status") == "invalid_key":
        print(f"[perchance] API returned invalid_key, waiting 3s for propagation then retrying...", file=sys.stderr)
        time.sleep(3)
        # First retry with same key (might just need propagation time)
        cache_bust_retry = str(random.random())
        url_retry = (
            f"{BASE}/generate"
            f"?userKey={user_key}"
            f"&requestId={str(random.random())}"
            f"&adAccessCode={AD_ACCESS_CODE}"
            f"&__cacheBust={cache_bust_retry}"
        )
        propagation_ok = False
        try:
            resp_retry = httpx.post(url_retry, content=json.dumps(body), headers=HEADERS, timeout=360)
            if resp_retry.status_code == 200:
                data_retry = resp_retry.json()
                if data_retry.get("status") != "invalid_key":
                    print("[perchance] Retry with same key succeeded after propagation delay", file=sys.stderr)
                    data = data_retry
                    propagation_ok = True
                else:
                    print("[perchance] Same key still invalid after propagation delay", file=sys.stderr)
            else:
                print(f"[perchance] Propagation retry got status {resp_retry.status_code}", file=sys.stderr)
        except Exception as e:
            print(f"[perchance] Propagation retry failed: {e}", file=sys.stderr)

        if not propagation_ok:
            # Still invalid — invalidate cache and get fresh key
            print("[perchance] Fetching new key via browser...", file=sys.stderr)
            if KEY_CACHE_FILE.exists():
                KEY_CACHE_FILE.unlink()
            try:
                user_key = get_valid_key()
            except RuntimeError as e:
                return {"ok": False, "error": str(e)}
            # Retry with fresh key
            cache_bust = str(random.random())
            url = (
                f"{BASE}/generate"
                f"?userKey={user_key}"
                f"&requestId={str(random.random())}"
                f"&adAccessCode={AD_ACCESS_CODE}"
                f"&__cacheBust={cache_bust}"
            )
            try:
                resp = httpx.post(url, content=json.dumps(body), headers=HEADERS, timeout=360)
                data = resp.json()
            except Exception as e:
                return {"ok": False, "error": f"Retry failed: {e}"}

    if data.get("status") != "success":
        return {"ok": False, "error": f"Generation failed: {json.dumps(data)[:300]}"}

    download_path = data.get("imageDownloadUrl", "")
    if not download_path:
        return {"ok": False, "error": "No download URL in response"}

    full_url = f"https://image-generation.perchance.org{download_path}"
    try:
        img_resp = httpx.get(full_url, headers=HEADERS, timeout=90, follow_redirects=True)
    except Exception as e:
        return {"ok": False, "error": f"Download failed: {e}"}

    if img_resp.status_code != 200 or len(img_resp.content) < 5000:
        return {"ok": False, "error": f"Download issue: status={img_resp.status_code}, size={len(img_resp.content)}"}

    ts = int(time.time())
    img_seed = data.get("seed", "unknown")
    filename = f"gen_{ts}_{img_seed}.jpg"
    out_path = OUTPUT_DIR / filename
    out_path.write_bytes(img_resp.content)

    return {
        "ok": True,
        "path": str(out_path),
        "filename": filename,
        "seed": img_seed,
        "width": data.get("width"),
        "height": data.get("height"),
        "size_bytes": len(img_resp.content),
    }


def generate_image(
    prompt: str,
    style: str = "no_style",
    shape: str = "square",
    count: int = 1,
    negative_prompt: str = "",
    seed: int = -1,
    key: str | None = None,
) -> dict:
    """Generate image(s) via Perchance API. Returns result dict with images array."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        user_key = get_valid_key(key)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}

    style_prefix = STYLES.get(style, "")
    full_prompt = f"{style_prefix}, {prompt}" if style_prefix else prompt
    resolution = SHAPES.get(shape, "768x768")
    neg = negative_prompt or DEFAULT_NEGATIVE
    count = max(1, min(count, 4))

    images = []
    errors = []
    for i in range(count):
        # Use provided seed for first image, random for rest
        img_seed = seed if (i == 0 and seed != -1) else -1
        result = _generate_single(full_prompt, neg, resolution, img_seed, user_key)
        if result.get("ok"):
            images.append(result)
        else:
            errors.append(f"Image {i+1}: {result.get('error', 'unknown')}")

    if not images:
        return {"ok": False, "error": "; ".join(errors) or "All generations failed"}

    return {
        "ok": True,
        "images": images,
        "count": len(images),
        "style": style,
        "shape": shape,
        "prompt_used": full_prompt,
        "errors": errors if errors else None,
    }


# ─── CLI ─────────────────────────────────────────────────────────────────────

def cmd_generate(args: argparse.Namespace) -> None:
    result = generate_image(
        prompt=args.prompt,
        style=args.style or "no_style",
        shape=args.shape or "square",
        count=args.count or 1,
        negative_prompt=args.negative_prompt or "",
        seed=args.seed if args.seed is not None else -1,
        key=getattr(args, "key", None),
    )
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("ok") else 1)


def cmd_save_key(args: argparse.Namespace) -> None:
    key = args.key.strip()
    if len(key) != 64:
        print(json.dumps({"ok": False, "error": f"Key must be 64 hex chars, got {len(key)}"}))
        sys.exit(1)
    if _verify_key(key):
        save_key(key)
        print(json.dumps({"ok": True, "message": "Key saved and verified"}))
    else:
        print(json.dumps({"ok": False, "error": "Key is invalid or expired"}))
        sys.exit(1)


def cmd_verify_key(args: argparse.Namespace) -> None:
    key = args.key.strip() if args.key else None
    try:
        valid_key = get_valid_key(key)
        print(json.dumps({"ok": True, "valid": True, "key": valid_key[:12] + "..."}))
    except RuntimeError as e:
        print(json.dumps({"ok": True, "valid": False, "error": str(e)}))


def cmd_list_styles(_args: argparse.Namespace) -> None:
    for name, prefix in STYLES.items():
        desc = prefix[:80] + "..." if len(prefix) > 80 else prefix
        print(f"  {name:20s} → {desc or '(raw prompt, no prefix)'}")


def cmd_styles_json(_args: argparse.Namespace) -> None:
    print(json.dumps(STYLES, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Perchance AI Image Generator")
    sub = parser.add_subparsers(dest="command")

    gen = sub.add_parser("generate", help="Generate an image")
    gen.add_argument("--prompt", required=True, help="Image description")
    gen.add_argument("--style", default="no_style", help="Style preset key")
    gen.add_argument("--shape", default="square", choices=list(SHAPES.keys()))
    gen.add_argument("--count", type=int, default=1, help="Number of images (1-4)")
    gen.add_argument("--negative-prompt", default="", help="Negative prompt")
    gen.add_argument("--seed", type=int, default=-1, help="Seed (-1 for random)")
    gen.add_argument("--key", default=None, help="Perchance userKey override")
    gen.set_defaults(func=cmd_generate)

    sk = sub.add_parser("save-key", help="Save and verify a Perchance userKey")
    sk.add_argument("key", help="64-char hex userKey")
    sk.set_defaults(func=cmd_save_key)

    vk = sub.add_parser("verify-key", help="Verify a Perchance userKey")
    vk.add_argument("key", nargs="?", default=None, help="Key to verify (default: check cached)")
    vk.set_defaults(func=cmd_verify_key)

    ls = sub.add_parser("list-styles", help="List available styles")
    ls.set_defaults(func=cmd_list_styles)

    sj = sub.add_parser("styles-json", help="Output styles as JSON")
    sj.set_defaults(func=cmd_styles_json)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
