
#!/usr/bin/env python3
"""
Integration tests for browser_control.py daemon.

Starts the daemon, runs through core commands against a local test page,
verifies responses, then shuts down. No external network needed — uses
a data: URI and a local HTML file for deterministic testing.

Run: cd /home/sifat/hdd/projects/Sable && uv run python test/test_browser_control.py
"""

import json
import os
import subprocess
import sys
import time
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "skills/core/browser_control/scripts/browser_control.py"
SOCKET = "/tmp/sable_browser.sock"
PID_FILE = "/tmp/sable_browser.pid"

PASS = 0
FAIL = 0


def run_cmd(*args, timeout=30) -> dict:
    """Send a command via the CLI client."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT)] + list(args),
        capture_output=True, text=True, timeout=timeout,
        cwd=str(SCRIPT.parent.parent.parent.parent),  # project root
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": f"Non-JSON output: {result.stdout[:200]} | stderr: {result.stderr[:200]}"}


def check(name: str, resp: dict, condition=None, expect_error=False):
    global PASS, FAIL
    if expect_error:
        ok = not resp.get("ok", True)  # We WANT ok=False
        if ok and condition:
            ok = condition(resp)
    else:
        ok = resp.get("ok", False)
        if condition and ok:
            ok = condition(resp.get("result", {}))
    if ok:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}")
        print(f"    Response: {json.dumps(resp, indent=2)[:300]}")


# ─── Test HTML ────────────────────────────────────────────────────────────────

TEST_HTML = """<!DOCTYPE html>
<html>
<head><title>Browser Control Test Page</title></head>
<body>
  <h1 id="title">Hello Sable</h1>
  <p class="intro">This is a test paragraph.</p>
  <button id="btn" onclick="document.getElementById('output').textContent='clicked!'">
    Click Me
  </button>
  <input id="name-input" type="text" placeholder="Type here" />
  <div id="output"></div>
  <a href="https://example.com" id="link">Example Link</a>
  <ul>
    <li class="item">Item One</li>
    <li class="item">Item Two</li>
    <li class="item">Item Three</li>
  </ul>
  <select id="color-select">
    <option value="red">Red</option>
    <option value="blue">Blue</option>
  </select>
</body>
</html>"""


def main():
    global PASS, FAIL

    # Write test HTML to temp file
    html_file = tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w")
    html_file.write(TEST_HTML)
    html_file.close()
    test_url = f"file://{html_file.name}"

    print("=" * 60)
    print("Browser Control — Integration Tests")
    print("=" * 60)

    # ─── Start Daemon ─────────────────────────────────────────────────────
    print("\n[Setup] Starting daemon (headless)...")
    daemon_proc = subprocess.Popen(
        [sys.executable, str(SCRIPT), "start", "--headless"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    # Wait for socket
    for _ in range(30):
        if os.path.exists(SOCKET):
            break
        time.sleep(0.3)
    else:
        print("FATAL: Daemon did not start (no socket after 9s)")
        daemon_proc.kill()
        sys.exit(1)

    time.sleep(0.5)  # extra settle
    print(f"  Daemon PID: {daemon_proc.pid}")

    try:
        # ─── Status ───────────────────────────────────────────────────────
        print("\n[Test] Status & Lifecycle")
        resp = run_cmd("status")
        check("status returns alive", resp, lambda r: r.get("alive") is True)

        # ─── Navigation ───────────────────────────────────────────────────
        print("\n[Test] Navigation")
        resp = run_cmd("open", test_url)
        check("open local file", resp, lambda r: "Browser Control Test" in (r.get("title") or ""))

        resp = run_cmd("reload")
        check("reload", resp, lambda r: r.get("url") == test_url)

        # ─── DOM Dump ─────────────────────────────────────────────────────
        print("\n[Test] DOM Inspection")
        resp = run_cmd("dump")
        check("dump returns tree/dom", resp,
              lambda r: "tree" in r or "dom" in r)

        resp = run_cmd("dump_html", "#title")
        check("dump_html specific selector", resp,
              lambda r: "Hello Sable" in (r.get("html") or ""))

        # ─── Extract ──────────────────────────────────────────────────────
        print("\n[Test] Extraction")
        resp = run_cmd("extract", "h1#title")
        check("extract h1 text", resp,
              lambda r: r.get("values", [None])[0] == "Hello Sable")

        resp = run_cmd("extract", ".item")
        check("extract multiple elements", resp,
              lambda r: r.get("found") == 3)

        resp = run_cmd("extract", "#link", "--attribute=href")
        check("extract attribute", resp,
              lambda r: "example.com" in (r.get("values", [""])[0] or ""))

        # ─── Click ────────────────────────────────────────────────────────
        print("\n[Test] Interaction")
        resp = run_cmd("click", "#btn")
        check("click button", resp, lambda r: r.get("clicked") == "#btn")

        # Verify side effect
        resp = run_cmd("extract", "#output")
        check("click side-effect verified", resp,
              lambda r: r.get("values", [""])[0] == "clicked!")

        # ─── Type ─────────────────────────────────────────────────────────
        resp = run_cmd("type", "#name-input", "Maria was here")
        check("type into input", resp, lambda r: r.get("text") == "Maria was here")

        # Verify via JS
        resp = run_cmd("eval", "document.getElementById('name-input').value")
        check("typed value confirmed via eval", resp,
              lambda r: r.get("result") == "Maria was here")

        # ─── Select ───────────────────────────────────────────────────────
        resp = run_cmd("select", "#color-select", "blue")
        check("select option", resp, lambda r: r.get("selected") == "blue")

        # ─── Eval ─────────────────────────────────────────────────────────
        print("\n[Test] JavaScript Eval")
        resp = run_cmd("eval", "document.title")
        check("eval document.title", resp,
              lambda r: r.get("result") == "Browser Control Test Page")

        resp = run_cmd("eval", "document.querySelectorAll('.item').length")
        check("eval querySelectorAll count", resp,
              lambda r: r.get("result") == 3)

        # ─── Screenshot ───────────────────────────────────────────────────
        print("\n[Test] Screenshot")
        resp = run_cmd("screenshot")
        check("screenshot saved", resp,
              lambda r: os.path.exists(r.get("path", "")) and r.get("size_kb", 0) > 1)

        resp = run_cmd("screenshot", "--selector=#title")
        check("element screenshot", resp,
              lambda r: os.path.exists(r.get("path", "")))

        # ─── Tabs ─────────────────────────────────────────────────────────
        print("\n[Test] Tab Management")
        resp = run_cmd("tabs")
        check("list tabs", resp, lambda r: r.get("tabs") and len(r["tabs"]) >= 1)

        resp = run_cmd("tab_new", test_url)
        check("open new tab", resp, lambda r: r.get("index", -1) >= 1)

        resp = run_cmd("tabs")
        check("two tabs now", resp, lambda r: len(r.get("tabs", [])) == 2)

        resp = run_cmd("tab_switch", "0")
        check("switch to tab 0", resp, lambda r: r.get("active") == 0)

        resp = run_cmd("tab_close", "1")
        check("close tab 1", resp, lambda r: r.get("remaining") == 1)

        # ─── Wait ─────────────────────────────────────────────────────────
        print("\n[Test] Wait")
        resp = run_cmd("wait", "#btn")
        check("wait for visible element", resp, lambda r: r.get("visible") == "#btn")

        # ─── DevTools: Page Source ─────────────────────────────────────────
        print("\n[Test] DevTools — Source & CSS")
        resp = run_cmd("source")
        check("source returns HTML", resp,
              lambda r: "Hello Sable" in (r.get("html") or "") and r.get("length", 0) > 100)

        resp = run_cmd("css", "#title")
        check("css computed styles", resp,
              lambda r: "font-size" in (r.get("styles") or {}))

        resp = run_cmd("css", "#title", "--properties=color,display")
        check("css specific properties", resp,
              lambda r: "color" in (r.get("styles") or {}) and "display" in (r.get("styles") or {}))

        resp = run_cmd("css", "#nonexistent-el")
        check("css missing selector errors", resp,
              lambda r: "error" in r and "not found" in r["error"].lower())

        # ─── DevTools: Storage & Cookies ───────────────────────────────────
        print("\n[Test] DevTools — Storage & Cookies")
        resp = run_cmd("eval", "localStorage.setItem('test_key', 'test_val_123')")
        check("set localStorage via eval", resp)

        resp = run_cmd("storage", "local")
        check("read localStorage", resp,
              lambda r: r.get("items", {}).get("test_key") == "test_val_123")

        resp = run_cmd("storage_set", "session", "--key=sess_k", "--value=sess_v")
        check("storage_set sessionStorage", resp,
              lambda r: r.get("set") == "sess_k")

        resp = run_cmd("storage", "session")
        check("read sessionStorage", resp,
              lambda r: r.get("items", {}).get("sess_k") == "sess_v")

        resp = run_cmd("cookies")
        check("cookies returns list", resp,
              lambda r: "count" in r and "cookies" in r)

        # ─── DevTools: Network Capture ─────────────────────────────────────
        print("\n[Test] DevTools — Network Capture")
        resp = run_cmd("network_start")
        check("network_start", resp, lambda r: r.get("capturing") is True)

        run_cmd("reload")
        time.sleep(0.5)

        resp = run_cmd("network_log")
        check("network_log has entries", resp,
              lambda r: r.get("total", 0) > 0 and len(r.get("entries", [])) > 0)

        resp = run_cmd("network_log", "--filter_type=document")
        check("network_log filter by type", resp,
              lambda r: all(e.get("resource_type") == "document"
                           for e in r.get("entries", []) if e.get("type") == "request")
              if r.get("entries") else True)

        resp = run_cmd("network_stop")
        check("network_stop", resp, lambda r: r.get("capturing") is False)

        resp = run_cmd("network_clear")
        check("network_clear", resp, lambda r: r.get("cleared") is True)

        # ─── DevTools: Console Capture ─────────────────────────────────────
        print("\n[Test] DevTools — Console Capture")
        resp = run_cmd("console_start")
        check("console_start", resp, lambda r: r.get("capturing") is True)

        run_cmd("eval", "console.log('maria_test_msg'); console.warn('warn_msg')")
        time.sleep(0.3)

        resp = run_cmd("console_log")
        check("console_log has entries", resp,
              lambda r: r.get("total", 0) >= 2)

        resp = run_cmd("console_log", "--level=warning")
        check("console_log filter by level", resp,
              lambda r: all(e.get("level") == "warning" for e in r.get("entries", []))
              if r.get("entries") else r.get("total", 0) >= 0)

        resp = run_cmd("console_stop")
        check("console_stop", resp, lambda r: r.get("capturing") is False)

        resp = run_cmd("console_clear")
        check("console_clear", resp, lambda r: r.get("cleared") is True)

        # ─── DevTools: Performance ─────────────────────────────────────────
        print("\n[Test] DevTools — Performance")
        resp = run_cmd("performance")
        check("performance returns timing", resp,
              lambda r: "navigation" in r and "resource_count" in r)

        # ─── Sequence ─────────────────────────────────────────────────────
        print("\n[Test] Sequence Execution")
        seq = [
            {"cmd": "open", "args": [test_url], "wait_ms": 200},
            {"cmd": "click", "args": ["#btn"], "wait_ms": 100},
            {"cmd": "type", "args": ["#name-input", "seq test"], "wait_ms": 100},
            {"cmd": "extract", "args": ["#output"], "wait_ms": 0},
        ]
        seq_file = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
        json.dump(seq, seq_file)
        seq_file.close()

        resp = run_cmd("seq", seq_file.name, timeout=30)
        check("seq runs all steps", resp,
              lambda r: r.get("steps_run") == 4)
        check("seq final step result", resp,
              lambda r: r.get("results", [{}])[-1].get("result", {}).get("values", [""])[0] == "clicked!")

        os.unlink(seq_file.name)

        # ─── Error Handling ───────────────────────────────────────────────
        print("\n[Test] Error Handling")
        resp = run_cmd("click", "#nonexistent-element")
        check("click missing element returns error", resp, expect_error=True)

        resp = run_cmd("eval", "this is not js {{{")
        check("bad JS returns error", resp, expect_error=True)

        resp = run_cmd("bogus_command")
        check("unknown command returns error", resp,
              condition=lambda r: "Unknown command" in (r.get("error") or ""),
              expect_error=True)

    finally:
        # ─── Shutdown ─────────────────────────────────────────────────────
        print("\n[Teardown] Stopping daemon...")
        run_cmd("stop")
        time.sleep(1)
        if daemon_proc.poll() is None:
            daemon_proc.kill()
        # Cleanup
        for f in [SOCKET, PID_FILE, html_file.name]:
            if os.path.exists(f):
                os.unlink(f)

    # ─── Summary ──────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    total = PASS + FAIL
    print(f"Results: {PASS}/{total} passed, {FAIL} failed")
    print("=" * 60)
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
