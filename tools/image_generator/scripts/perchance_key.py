"""Shared Perchance key management.

Perchance's verifyUser API is protected by Cloudflare Turnstile.  Raw httpx
calls *sometimes* work (CF is intermittent) but often return
``{"status":"failed_verification","reason":"token_required"}``.

This module provides:
- ``get_valid_key(cache_file)`` — returns a valid 64-char userKey or raises.
- Shared cache load/save/verify helpers.
- Fast-path: httpx GET to verifyUser (~1s, works when CF is relaxed).
- Fallback: browser-based key refresh via CDP + Chrome (~15-20s, always works).
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

BASE_URL = "https://image-generation.perchance.org/api"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "Referer": "https://image-generation.perchance.org/embed",
    "Content-Type": "text/plain;charset=UTF-8",
}


# ─── Cache helpers ───────────────────────────────────────────────────────────

def load_cached_key(cache_file: Path) -> str | None:
    """Load key from cache file (JSON ``{"userKey": "..."}`` or raw hex)."""
    if not cache_file.exists():
        return None
    try:
        raw = cache_file.read_text().strip()
        if not raw:
            return None
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data.get("userKey")
        except (json.JSONDecodeError, TypeError):
            pass
        if len(raw) == 64:
            return raw
    except Exception:
        pass
    return None


def save_key(key: str, cache_file: Path) -> None:
    """Persist a key to *cache_file* as JSON."""
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps({"userKey": key.strip()}, indent=2))


def verify_key(key: str) -> bool:
    """Check whether *key* is still valid via ``checkVerificationStatus``."""
    if httpx is None:
        return False
    try:
        url = f"{BASE_URL}/checkVerificationStatus?userKey={key}&__cacheBust={random.random()}"
        resp = httpx.get(url, headers=HEADERS, timeout=10)
        data = resp.json()
        return data.get("status") == "verified"
    except Exception:
        return False


# ─── HTTP fast-path (intermittent — CF sometimes allows it) ─────────────────

def _refresh_key_via_http(tag: str = "perchance") -> str | None:
    """Try to get a fresh userKey via direct httpx GET to verifyUser.

    This works when Cloudflare's Turnstile protection is relaxed for the
    current IP/time window.  Returns ``None`` on failure (token_required,
    network error, etc.).  Takes ~1s when successful.
    """
    if httpx is None:
        return None
    try:
        url = f"{BASE_URL}/verifyUser?thread=0&__cacheBust={random.random()}"
        resp = httpx.get(url, headers=HEADERS, timeout=10)
        data = resp.json()
        key = data.get("userKey", "")
        status = data.get("status", "")
        reason = data.get("reason", "")
        if key and len(key) == 64:
            print(f"[{tag}][key] httpx fast-path succeeded: {key[:16]}...", file=sys.stderr)
            return key
        print(f"[{tag}][key] httpx fast-path failed: status={status}, reason={reason}", file=sys.stderr)
    except Exception as e:
        print(f"[{tag}][key] httpx fast-path error: {e}", file=sys.stderr)
    return None


# ─── Browser-based key refresh ──────────────────────────────────────────────

def refresh_key_via_browser(tag: str = "perchance") -> str | None:
    """Launch a stealth Chrome instance, let Perchance solve Turnstile, and
    capture the resulting ``userKey`` from network traffic.

    Returns the 64-char key or ``None`` on failure.
    """
    import shutil

    print(f"[{tag}][browser] === Starting browser key refresh ===", file=sys.stderr)

    # ── Find Chrome ──
    browser_bin = None
    for name in ["helium-browser", "google-chrome", "google-chrome-stable", "chromium"]:
        path = shutil.which(name)
        if path:
            browser_bin = path
            print(f"[{tag}][browser] Found browser: {name} -> {path}", file=sys.stderr)
            break
    if not browser_bin:
        for p in ["/opt/helium-browser-bin/chrome", "/usr/bin/google-chrome"]:
            if os.path.isfile(p) and os.access(p, os.X_OK):
                browser_bin = p
                print(f"[{tag}][browser] Found browser at hardcoded path: {p}", file=sys.stderr)
                break
    if not browser_bin:
        print(f"[{tag}][browser] ERROR: No Chrome browser found", file=sys.stderr)
        return None

    # ── Free CDP port ──
    cdp_port = 9222
    for port in range(9222, 9230):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                cdp_port = port
                break
    print(f"[{tag}][browser] CDP port: {cdp_port}", file=sys.stderr)

    import tempfile
    user_data_dir = os.path.join(tempfile.gettempdir(), f"perchance_key_refresh_{os.getpid()}")

    # ── Display env (Wayland, POSIX only) ──
    browser_env = os.environ.copy()
    if sys.platform != "win32" and not browser_env.get("WAYLAND_DISPLAY"):
        import glob as _glob
        uid = os.getuid()
        sockets = sorted(_glob.glob(f"/run/user/{uid}/wayland-[0-9]*"))
        sockets = [
            s for s in sockets
            if os.path.basename(s) != f"wayland-{os.path.basename(s)}.lock"
            and not s.endswith(".lock") and not s.endswith(".sock")
        ]
        if sockets:
            browser_env["WAYLAND_DISPLAY"] = os.path.basename(sockets[0])
        else:
            browser_env["WAYLAND_DISPLAY"] = "wayland-0"

    cmd = [
        browser_bin,
        f"--remote-debugging-port={cdp_port}",
        "--disable-blink-features=AutomationControlled",
        "--no-first-run", "--no-default-browser-check", "--no-sandbox",
        "--ozone-platform=wayland",
        f"--user-data-dir={user_data_dir}",
        "about:blank",
    ]
    print(f"[{tag}][browser] Launching: {' '.join(cmd)}", file=sys.stderr)

    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=browser_env
    )

    # ── Wait for CDP ──
    import urllib.request
    cdp_ready = False
    for attempt in range(30):
        try:
            resp = urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json/version", timeout=1)
            json.loads(resp.read())
            cdp_ready = True
            break
        except Exception:
            if proc.poll() is not None:
                stderr_out = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                print(f"[{tag}][browser] ERROR: Browser exited ({proc.poll()}) before CDP ready. stderr: {stderr_out[:500]}", file=sys.stderr)
                return None
            time.sleep(0.2)

    if not cdp_ready:
        print(f"[{tag}][browser] ERROR: CDP not ready after 30 attempts", file=sys.stderr)
        proc.kill()
        return None

    captured_key: str | None = None
    try:
        captured_key = asyncio.run(_async_capture_key(cdp_port, tag))
    except Exception as e:
        import traceback
        print(f"[{tag}][browser] ERROR: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(user_data_dir, ignore_errors=True)

    if captured_key:
        print(f"[{tag}][browser] Key captured: {captured_key[:16]}...", file=sys.stderr)
    else:
        print(f"[{tag}][browser] WARNING: No key captured", file=sys.stderr)
    return captured_key


async def _async_capture_key(cdp_port: int, tag: str = "perchance") -> str | None:
    """Connect to Chrome via CDP, navigate to Perchance, capture userKey.

    Uses Playwright's native page.on('request') for network capture (sees
    ALL frames including nested iframes) and frame.evaluate() for DOM
    interaction.  No raw CDP websockets — avoids message ID sync issues.
    """
    from playwright.async_api import async_playwright
    from urllib.parse import urlparse, parse_qs

    await asyncio.sleep(3)
    captured: str | None = None
    captured_event = asyncio.Event()

    def _on_request(request):
        nonlocal captured
        if captured:
            return
        url = request.url
        if "userKey" not in url:
            return
        qs = parse_qs(urlparse(url).query)
        if "userKey" in qs:
            key = qs["userKey"][0]
            if len(key) == 64:
                captured = key
                captured_event.set()
                print(f"[{tag}][capture] ✓ CAPTURED userKey: {key[:16]}...", file=sys.stderr)

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
        if not browser.contexts:
            print(f"[{tag}][capture] ERROR: No browser contexts", file=sys.stderr)
            return None

        context = browser.contexts[0]
        page = await context.new_page()

        # Listen for requests BEFORE navigation (catches everything)
        page.on("request", _on_request)

        print(f"[{tag}][capture] Navigating to generator page...", file=sys.stderr)
        try:
            await page.goto(
                "https://perchance.org/ai-text-to-image-generator",
                wait_until="domcontentloaded",
                timeout=60000,
            )
        except Exception as e:
            print(f"[{tag}][capture] Navigation issue (may be ok): {e}", file=sys.stderr)

        await asyncio.sleep(5)

        # Key may already be captured during page load
        if captured:
            print(f"[{tag}][capture] Key captured during page load", file=sys.stderr)
            await page.close()
            return captured

        # Find the generator iframe via Playwright frames
        gen_frame = None
        for frame in page.frames:
            furl = frame.url
            if "perchance.org" in furl and "ai-text-to-image" in furl and frame != page.main_frame:
                gen_frame = frame
                print(f"[{tag}][capture] Found generator frame: {furl[:80]}", file=sys.stderr)
                break

        if not gen_frame:
            print(f"[{tag}][capture] ERROR: No generator iframe found", file=sys.stderr)
            await page.close()
            return None

        # Clear localStorage and reload to force fresh key
        try:
            await gen_frame.evaluate("localStorage.clear()")
            print(f"[{tag}][capture] Cleared localStorage", file=sys.stderr)
            await gen_frame.evaluate("location.reload()")
            await asyncio.sleep(5)
        except Exception as e:
            print(f"[{tag}][capture] Clear/reload failed: {e}", file=sys.stderr)

        if captured:
            print(f"[{tag}][capture] Key captured after reload", file=sys.stderr)
            await page.close()
            return captured

        # Type "test" and click generate
        print(f"[{tag}][capture] Typing 'test' and clicking generate...", file=sys.stderr)
        trigger_result = await gen_frame.evaluate("""
            (() => {
                var ta = null;
                var tas = document.querySelectorAll('textarea.paragraph-input');
                for (var i = 0; i < tas.length; i++) {
                    if (tas[i].offsetParent !== null) { ta = tas[i]; break; }
                }
                if (!ta) ta = document.querySelector('textarea');
                if (!ta) return JSON.stringify({error: 'no-textarea'});

                ta.focus();
                document.execCommand('selectAll', false, null);
                document.execCommand('insertText', false, 'test');

                var btns = document.querySelectorAll('button, input[type=submit], [onclick]');
                var clicked = false;
                for (var j = 0; j < btns.length; j++) {
                    var b = btns[j];
                    if (/generat/i.test(b.textContent || b.value || '')) {
                        b.click();
                        clicked = true;
                        break;
                    }
                }
                return JSON.stringify({typed: ta.value.substring(0,20), clicked: clicked});
            })()
        """)
        print(f"[{tag}][capture] Trigger result: {trigger_result}", file=sys.stderr)

        # Wait for key capture
        try:
            await asyncio.wait_for(captured_event.wait(), timeout=60)
        except asyncio.TimeoutError:
            print(f"[{tag}][capture] Timed out waiting for userKey", file=sys.stderr)

        await page.close()

    return captured


# ─── Public API ──────────────────────────────────────────────────────────────

def get_valid_key(
    cache_file: Path,
    *,
    override_key: str | None = None,
    env_var: str = "PERCHANCE_KEY",
    tag: str = "perchance",
) -> str:
    """Return a valid 64-char Perchance userKey.

    Priority: *override_key* → env var → cached file → browser refresh.
    Raises ``RuntimeError`` if all methods fail.
    """
    candidates: list[tuple[str, str]] = []
    if override_key:
        candidates.append(("override", override_key.strip()))
    env_key = os.environ.get(env_var, "")
    if env_key:
        candidates.append(("env", env_key.strip()))
    cached = load_cached_key(cache_file)
    if cached:
        candidates.append(("cache", cached))

    for source, key in candidates:
        if len(key) != 64:
            continue
        if verify_key(key):
            save_key(key, cache_file)
            print(f"[{tag}][key] Using {source} key: {key[:16]}...", file=sys.stderr)
            return key

    # All cached keys invalid → try httpx fast-path first (~1s), then browser (~15-20s)
    print(f"[{tag}][key] All cached keys invalid, trying httpx fast-path...", file=sys.stderr)
    new_key = _refresh_key_via_http(tag)
    if new_key and len(new_key) == 64:
        save_key(new_key, cache_file)
        return new_key

    print(f"[{tag}][key] httpx fast-path failed, falling back to browser...", file=sys.stderr)
    new_key = refresh_key_via_browser(tag)
    if new_key and len(new_key) == 64:
        save_key(new_key, cache_file)
        return new_key

    raise RuntimeError(
        f"No valid Perchance userKey found and browser refresh failed.\n"
        f"Set one via: --key YOUR_KEY, {env_var} env var, or ensure a Chrome browser is available."
    )
