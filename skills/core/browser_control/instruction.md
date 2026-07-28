# Browser Control — Persistent Playwright Daemon

Headless (or headed) browser automation via a persistent Chromium/Firefox/WebKit
daemon. Holds a live browser instance between calls — no cold-start per command.
Communicates over a Unix socket (`/tmp/sable_browser.sock`). Full DevTools
capability: network capture, console logging, CSS inspection, storage, cookies,
performance timing.

---

## 🚨 CRITICAL RULES — READ BEFORE ANY BROWSER TASK

### Rule 1: Shell Quoting Will Kill Your eval Commands

**NEVER** pass JavaScript with double quotes directly in the shell. Bash eats
them. Every `eval 'document.querySelector("div")'` becomes a syntax error.

Three safe patterns (use one of these, always):

| Pattern | When | Example |
|---|---|---|
| `eval --b64 BASE64STRING` | Complex JS with quotes/newlines/emoji | Write JS to file, `base64 -w0 /tmp/x.js`, pass the string |
| `eval --file /tmp/x.js` | JS already saved to a file | `eval --file /tmp/gmail_body.js` |
| `eval 'simple()'` | Trivial JS with **NO** double quotes inside | `eval 'document.title'` |

**The b64 workflow (memorize this):**

```bash
# 1. Write JS to file (heredoc with quoted delimiter = zero shell interference)
cat > /tmp/my_eval.js << 'JSEOF'
var b = document.querySelector('div[aria-label="Message Body"]');
b.focus();
document.execCommand('insertText', false, "Hello world");
JSON.stringify({len: b.innerText.length})
JSEOF

# 2. Encode and pass
B64=
cd PROJECT_ROOT && uv run python skills/core/browser_control/scripts/browser_control.py eval "b64:"
```

Or skip the shell variable entirely:

```bash
cd PROJECT_ROOT && uv run python skills/core/browser_control/scripts/browser_control.py eval --file /tmp/my_eval.js
```

> [!CAUTION]
> If an eval returns `SyntaxError: missing ) after argument list` — it is shell
> quoting. **Stop.** Switch to `--b64` or `--file`. Do NOT retry with different
> quote arrangements.

### Rule 2: Headed Mode + Visible Window Required for Interaction

`click`, `type`, `hover`, `select` all run Playwright actionability checks:
the element must be **visible, enabled, and stable** in the viewport.

If the browser window is minimized, on another workspace, or occluded, every
interaction command times out with `element is not visible` — even though the
element exists in the DOM and `eval` works fine.

**Symptoms of hidden window:**

- `eval` commands succeed but `click`/`type` timeout after 10s
- Error says `element is not visible` repeatedly
- `dump_html` shows the element exists

**Fix:** Restart the daemon with `--headed` and ensure the window is visible.
Or use `--force=true` on click/type to skip visibility checks:

```bash
cd PROJECT_ROOT && uv run python skills/core/browser_control/scripts/browser_control.py click "div[aria-label='Message Body']" --force=true
```

> [!WARNING]
> `--force=true` skips ALL safety checks. Only use when you have confirmed the
> element exists via `eval` or `dump_html` and the window is just not visible.

### Rule 3: Diagnose Before Retry

**Never retry the same failing command more than once.** If a command fails:

1. **Read the actual error message.** It tells you what is wrong.
2. **Check page state:** `eval 'document.title'` — are you on the right page?
3. **Check element existence:** `dump_html "selector"` or `eval 'document.querySelector("sel")?.outerHTML'`
4. **Check visibility:** if `eval` finds the element but `click`/`type` cannot → window is hidden (Rule 2)
5. **Screenshot:** `screenshot` then view the path to actually SEE the page

> [!FAILURE]
> The retry loop of death: command fails → retry same command → fails again →
> try slight variation → fails → try another variation → 30 minutes wasted.
> **Stop after one failure. Diagnose. Then fix.**

---

## Trigger Guard

| Condition | Action |
|---|---|
| "open [url]" / "go to [site]" / "navigate" | Use `open` |
| "click [selector]" / "tap [element]" on a webpage | Use `click` |
| "type [text] into [field]" on a webpage | Use `type` |
| "screenshot" / "capture the page" | Use `screenshot` |
| "what is on the page" / "get the HTML" / "dump the DOM" | Use `dump` or `source` |
| "extract [data]" / "scrape [elements]" | Use `extract` |
| "check the network requests" / "what API calls is it making" | Use `network_start` → action → `network_log` → `network_stop` |
| "check console errors" / "any JS errors" | Use `console_start` → action → `console_log` → `console_stop` |
| "what CSS does [element] have" / "computed styles" | Use `css` |
| "check localStorage" / "read cookies" / "storage" | Use `storage` / `cookies` |
| "page load time" / "performance" / "slowest resources" | Use `performance` |
| "run JS" / "evaluate" / "execute script on page" | Use `eval` (with `--b64` or `--file` for complex JS) |
| Daemon not running / socket missing | Run `start` first |

---

## When to Use seq vs Direct Commands

| Use `seq` (JSON file) | Use direct commands |
|---|---|
| Repeatable multi-step workflow (scrape pipeline, form fill + submit) | Interactive debugging / exploration |
| 4+ steps that always run together | 1-3 steps where you need to inspect results between steps |
| You want atomic abort-on-first-failure | You need to adapt the next step based on the previous result |
| Running in background (nohup) | Running inline with immediate feedback |

> [!IMPORTANT]
> Do NOT default to `seq` for everything. If you are exploring a page, debugging
> a selector, or need to read results between steps — use direct commands. Seq is
> for defined, repeatable workflows where you know all steps upfront.

---

## Script Path

`PROJECT_ROOT/skills/core/browser_control/scripts/browser_control.py`

Always call via:

```bash
cd PROJECT_ROOT && uv run python skills/core/browser_control/scripts/browser_control.py COMMAND [args]
```

---

## Daemon Lifecycle

| Command | Description |
|---|---|
| `start` | Launch daemon (headless by default). Add `--headed` for visible window. |
| `start --browser=firefox` | Use Firefox engine instead of Chromium. |
| `stop` | Gracefully shut down daemon + browser. |
| `status` | Health check — returns alive, url, title, tab count. |

One daemon = one browser. To switch browsers: `stop` then `start --browser=X`.
Socket path is fixed at `/tmp/sable_browser.sock`.

**Start command (background, headed):**

```bash
setsid /home/sifat/hdd/projects/Sable/.venv/bin/python /home/sifat/hdd/projects/Sable/skills/core/browser_control/scripts/browser_control.py start --headed --user-data-dir=/home/sifat/hdd/projects/Sable/browser-scraper-data > /tmp/browser_daemon.log 2>&1 &
sleep 5
```

Then verify: `status`. If daemon is not running, ALL commands fail.

> [!TIP]
> Always `stop` the daemon when browser work is complete.

---

## Navigation

| Command | Args | Description |
|---|---|---|
| `open URL` | url | Navigate to URL. Returns title + final URL. |
| `reload` | — | Reload current page. |
| `back` | — | Go back in history. |
| `forward` | — | Go forward in history. |

---

## DOM Inspection

| Command | Args | Description |
|---|---|---|
| `dump` | — | Accessibility tree / DOM summary of page. |
| `dump_html [selector]` | optional CSS selector | Raw innerHTML. Full page if no selector. |
| `source` | — | Full page HTML source (truncated at 30k chars). |
| `extract SELECTOR` | CSS selector, `--attr=name` | Extract text (or attribute) from matching elements. Returns `values[]` + found count. |
| `query SELECTOR` | CSS selector | First matching element info. |
| `query_all SELECTOR` | CSS selector | All matching elements. |

---

## Interaction

| Command | Args | Description |
|---|---|---|
| `click SELECTOR` | CSS selector, `--force=true` | Click element. Waits for visibility (skip with force). |
| `type SELECTOR TEXT` | CSS selector, text, `--force=true` | Clear + type into input/contenteditable. |
| `select SELECTOR VALUE` | CSS selector, option value | Select dropdown option. |
| `hover SELECTOR` | CSS selector | Hover over element. |
| `press KEY` | key name (`Enter`, `Tab`, `Control+Enter`, etc) | Press keyboard key. |
| `eval JS` | JS string, `b64:ENCODED`, or `--file /path.js` | Execute JS in page context. |

### eval Input Modes

| Mode | Syntax | Use when |
|---|---|---|
| Raw | `eval 'document.title'` | JS has NO double quotes |
| Base64 | `eval "b64:dmFyIHggPSAxOw=="` | JS has quotes, newlines, emoji, special chars |
| File | `eval --file /tmp/script.js` | JS is already in a file |

> [!TIP]
> Base64 is the default for anything beyond trivial JS. Do not fight shell
> quoting. Write JS to file → base64 encode → pass. Done.

---

## Screenshot

| Command | Args | Description |
|---|---|---|
| `screenshot` | `--selector=css`, `--full=true` | Save PNG to `/tmp/sable_browser_screenshots/`. Returns path + size. |

Use the file_uploader skill (get_file tag) with the screenshot path to view it.

---

## Tab Management

| Command | Args | Description |
|---|---|---|
| `tabs` | — | List all tabs (index, url, title) + active index. |
| `tab_new [url]` | optional url | Open new tab, optionally navigate. |
| `tab_switch INDEX` | tab index | Switch active tab. |
| `tab_close [index]` | optional index (default: active) | Close tab. |

---

## DevTools: Network Capture

| Command | Args | Description |
|---|---|---|
| `network_start` | — | Begin capturing requests + responses. |
| `network_stop` | — | Stop capturing. Returns entry count. |
| `network_log` | `--filter_type=xhr`, `--filter_url=api`, `--limit=50` | Retrieve captured entries. |
| `network_clear` | — | Clear the capture buffer. |

**Workflow:** `network_start` → perform actions → `network_log` → `network_stop`.

---

## DevTools: Console Capture

| Command | Args | Description |
|---|---|---|
| `console_start` | — | Begin capturing console messages. |
| `console_stop` | — | Stop capturing. Returns entry count. |
| `console_log` | `--level=error`, `--limit=50` | Retrieve messages. Filter by level. |
| `console_clear` | — | Clear the capture buffer. |

---

## DevTools: CSS Inspection

| Command | Args | Description |
|---|---|---|
| `css SELECTOR` | CSS selector, `--properties=color,font-size` | Get computed styles. |

Returns `{"selector": "...", "styles": {"property": "value", ...}}`.
If selector not found, returns `{"error": "Selector not found: ..."}` inside result (`ok=true`).

---

## DevTools: Storage and Cookies

| Command | Args | Description |
|---|---|---|
| `storage KIND` | `local` or `session` | Read all key-value pairs (values truncated at 200 chars). |
| `storage_set KIND` | `--key=name`, `--value=val` | Set a storage item. |
| `cookies [url]` | optional URL filter | Dump cookies (max 50). |

---

## DevTools: Performance

| Command | Args | Description |
|---|---|---|
| `performance` | — | Navigation timing + top 10 slowest resources. |

---

## Wait

| Command | Args | Description |
|---|---|---|
| `wait SELECTOR` | CSS selector, `--timeout=15000` | Wait until element is visible. |
| `wait_url SUBSTRING` | URL substring, `--timeout=15000` | Wait until URL contains substring. |
| `wait_load [state]` | `networkidle` (default), `load`, `domcontentloaded` | Wait for page load state. |

---

## Sequence Execution

For defined, repeatable multi-step workflows. Write a JSON array of steps,
then call `seq PATH`.

```json
[
  {"cmd": "open", "args": ["https://example.com"], "wait_ms": 500},
  {"cmd": "click", "args": ["#login-btn"], "wait_ms": 1000},
  {"cmd": "screenshot", "args": [], "wait_ms": 0}
]
```

Aborts on first failure. Returns all step results with pass/fail status.
Timeout: 120s. Run in background (`nohup`) if total `wait_ms` exceeds 10s.

> [!IMPORTANT]
> Seq is NOT the default. Use direct commands for interactive work. See
> "When to Use seq vs Direct Commands" above.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `SyntaxError: missing ) after argument list` on eval | Shell ate double quotes in JS | Use `--b64` or `--file` (Rule 1) |
| `element is not visible` on click/type (10s timeout) | Browser window minimized/hidden | Restart headed, or use `--force=true` (Rule 2) |
| `element is not visible` but eval finds the element | Same — window not visible to Playwright | `--force=true` or restart daemon headed |
| `TimeoutError: Page.fill` on contenteditable div | `fill()` targets inputs; div needs focus first | `click` the div first (with `--force=true` if needed), then `eval` with `execCommand('insertText')` |
| `TrustedHTML` error on innerHTML | Site CSP blocks innerHTML assignment | Use `document.execCommand('insertText')` or `createTextNode + appendChild` |
| `execCommand` returns true but text does not appear | Site silently rejects (Gmail TrustedHTML) | Use `createTextNode + appendChild + dispatch input event` |
| Daemon not running | Socket missing or process dead | `start` the daemon; check `/tmp/browser_daemon.log` |
| Daemon already running but commands fail | Stale socket from crashed process | `kill ; rm -f /tmp/sable_browser.sock /tmp/sable_browser.pid` then restart |
| Seq command killed at 15s | Shell timeout less than seq duration | Run with `nohup ... &` and check log after |
| `locator resolved to N elements` | Selector matches multiple elements | Make selector more specific, or use `eval` to target by index |

---

## Gmail / Contenteditable-Specific Notes

Gmail compose body is a `contenteditable` div with a TrustedHTML CSP policy:

- `innerHTML =` → throws TrustedHTML error
- `execCommand('insertText')` → returns true but may silently discard text
- `createTextNode + appendChild` → works, but Gmail may not register it as user input

**Most reliable:** `click` the body div (with `--force=true` if needed) →
`eval --file` with `execCommand('selectAll')` then `execCommand('insertText', false, text)` →
verify with `eval 'el.innerText.length'`

Gmail To field uses a peoplekit combobox — `type` works on a fresh compose,
but if a recipient chip already exists, the input may be hidden. Use `eval` to
check state first.

**Gmail send:** `press "Control+Enter"` is the universal send shortcut. Clicking the
Send button often fails (multiple matches, visibility). Always verify send by
checking `document.title` returns to inbox.

---

## Tips

- Always start the daemon first. Check with `status` if unsure.
- Network/console capture is opt-in. Must call `*_start` before actions, `*_log` after.
- Use `eval --file` for anything complex — write JS to `/tmp`, pass the path. Zero quoting issues.
- Screenshots go to `/tmp/sable_browser_screenshots/` — auto-created.
- For long-running tasks, launch daemon `start` as a background command since browser launch takes 2-3s.
- Always `stop` the daemon when work is complete.
- **Persistent sessions:** the daemon defaults to `PROJECT_ROOT/automation-browser-data` as its profile directory (cookies, logins persist). Override with `--user-data-dir=/path`.
- **Zen/Firefox incompatibility:** Playwright requires its own bundled browsers. Do NOT pass `--executable` pointing to Zen/Firefox.
