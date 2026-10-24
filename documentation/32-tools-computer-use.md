# 32 — `tools/computer_use/scripts/computer_use.py`

> [!INFO] Why this file exists
> Priority 1 on the README's coverage list. **1,549 lines** — the largest single tool, and the only one that touches the real desktop. [[29-engine-skills-handlers]] documents `handle_computer_use`, which is a 90-line `subprocess.run` wrapper around this file. The 1,549 lines behind it were never opened.
>
> Read end to end.

***

## 1. Structure

One CLI: `python computer_use.py '<json>'`. Dispatch is a dict lookup, then `getattr`:

```python
handler = _BROWSER_ACTIONS.get(action)       # web_screenshot, web_browser
if handler is None:
    backend = get_backend()                   # platform-selected
    handler = getattr(backend, action, None)
```

Three control backends + three screen parsers, paired by `CompositeBackend`:

| Platform | Control | Parser | Mechanism |
|:--|:--|:--|:--|
| Linux X11 | `X11Backend` | `X11ScreenParser` | xdotool, scrot/maim, xclip, wmctrl |
| Linux Wayland | `WaylandBackend` | `WaylandScreenParser` | ydotool/wtype, grim, wl-clipboard, hyprctl |
| Windows | `WindowsBackend` | `WindowsScreenParser` | PowerShell + .NET interop, UIA |

`CompositeBackend.__getattr__` routes `screen_parse` to the parser and everything else to the control backend. Because `__getattr__` only fires on a failed normal lookup, the split is clean — no explicit delegation table to keep in sync.

Two actions bypass the backends entirely (`_BROWSER_ACTIONS`): `web_screenshot` and `web_browser`, both Playwright-based and platform-independent.

***

## 2. The Wayland backend is the most carefully written

Every failure path returns a structured `{"error": …}` instead of raising, and every one names its likely cause:

```python
ok, err = run_checked(["ydotool", "mousemove", "--absolute", "-x", str(x), "-y", str(y)])
if not ok:
    return {"error": f"mousemove failed: {err} (is ydotoold running?)"}
```

`run_checked` is a two-line helper whose docstring says exactly the right thing — *"Never swallow failures"* — and it returns `(ok, message)` rather than a bool, so the message reaches the caller.

Three details worth recording because they are non-obvious and correct:

- **`-D 30` between down and up** on `ydotool click`, with the comment *"so QML/GTK widgets register a real tap"*. A synthetic click with no dwell is dropped by some toolkits.
- **`mouse_drag` interpolates 12 steps.** `ydotool` has no drag primitive, so it does explicit `mousemove` between press and release. A two-point jump does not trigger drag-and-drop handlers that track pointer movement; interpolated motion does. The comment says so.
- **Keycodes are raw Linux input-event-codes**, not key names, with a `MOD_KEYS` set that is held down across the sequence and released in reverse order at the end. That is the correct shape for `ydotool key`, which has no chord syntax.

`mouse_scroll` is honest about a real limitation rather than hiding it:

> *"ydotool 1.0.4 has no wheel command — emulate with arrow keys"*

It multiplies `amount` by 3 and sends alternating down/up key events. That scrolls a *focused* pane, not the pane under the cursor, which is a behavioural difference from X11 — documented, not hidden.

***

## 3. `run_cmd` decodes output as text unconditionally

```python
def run_cmd(cmd, shell=False, timeout=15) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=shell, capture_output=True, text=True, timeout=timeout)
```

`text=True` with no `errors=` means Python decodes stdout/stderr as UTF-8 and **raises `UnicodeDecodeError` on invalid bytes**. That is fine for `xdotool getdisplaygeometry`; it is not fine for `clipboard_get`:

```python
def clipboard_get(self, params):
    r = run_cmd(["xclip", "-selection", "clipboard", "-o"])
```

Copying an image, a PDF, or any binary blob to the clipboard and then calling `clipboard_get` raises inside `subprocess.run` — not caught here, so it propagates to `main()`'s `except Exception` and returns `{"error": "'utf-8' codec can't decode byte…"}`. The user gets a decode error instead of the clipboard contents.

The sibling `clipboard_set` uses `subprocess.run(..., input=text, text=True)` — and no `timeout=`. A hung `xclip`/`wl-copy` blocks the tool call indefinitely; `handle_computer_use` passes a `timeout` to its own `subprocess.run`, so the outer wrapper does eventually kill it, but the inner call has no deadline of its own.

> [!WARNING] Ledger #75
> `errors="replace"` on `run_cmd` fixes the decode failure for every caller at once. `clipboard_set` needs `timeout=` separately.

***

## 4. `WindowsBackend.keyboard_type` sends the text through SendKeys

```python
text = params.get("text", "").replace("'", "''")
script = f"""...
[System.Windows.Forms.SendKeys]::SendWait('{text}')
"""
```

The `.replace("'", "''")` correctly escapes the *PowerShell* single-quoted string, so there is no injection there. But `SendKeys` has its own metacharacters:

| Char | SendKeys meaning |
|:--|:--|
| `+` | Shift (modifier) |
| `^` | Ctrl (modifier) |
| `%` | Alt (modifier) |
| `~` | Enter |
| `( ) { } [ ]` | grouping / special keys |

So `keyboard_type({"text": "c++"})` does not type `c++` — SendKeys reads the two `+` as Shift modifiers applied to the following characters. `keyboard_type({"text": "(see notes)"})` opens a group. `keyboard_type({"text": "100%"})` turns the rest of the string into Alt-modified keystrokes.

The fix is to brace every metacharacter: `re.sub(r'([+^%~(){}[\]])', r'{\1}', text)` before embedding. That is the documented escape for a literal SendKeys character.

`keyboard_key` has a smaller version of the same issue — `keys.lower().split("+")` means a key literally named with a plus (e.g. `ctrl++`) cannot be expressed, and an unknown key name falls through to `key_map.get(last, last)` and is sent as a bare string.

> [!CAUTION] Ledger #76 — silent wrong output
> This one does not error; it types something. A model told to "type the formula 2+2" produces `22` with Shift held. Silent wrongness in the one action whose entire contract is faithful input.

***

## 5. `_spawn_browser` has no lock, so two callers can both launch

```python
def _spawn_browser(port, headless, user_data_dir=None):
    if _port_open(port):
        ...return reused...
    binary = _find_chrome_bin()
    ...
    proc = subprocess.Popen(cmd, ..., start_new_session=(os.name != "nt"))
```

Check-then-act with no mutex. Two `web_screenshot(keep_alive=true)` calls arriving together both see the port closed, both spawn a Chrome, and the second one loses the `SingletonLock` race and exits — but `_write_state` has already been called by whichever finished last, so `state.json` may point at a pid that is already dead.

Sable runs sync tool calls in a threadpool, so concurrent invocations are the normal case, not a hypothetical. The `browser_scraper.py` equivalent guards this with the governor's `acquire()`; this file has no equivalent.

Related: `_pid_alive` uses `os.kill(pid, 0)` — the exact test `browser_scraper.py:§3.1` documents as **not** a liveness check, because a zombie answers it. So `_stop_browser` can loop its full 5 s on an already-dead process before sending SIGKILL to a group that no longer exists.

***

## 6. The headless GPU flags can produce a blank screenshot

```python
_GPU_ARGS = [
    "--use-angle=gl",
    "--enable-webgl",
    "--ignore-gpu-blocklist",
    "--enable-gpu-rasterization",
    "--enable-zero-copy",
    "--disable-software-rasterizer",
]
```

The comment explains why they exist: headless Chrome otherwise falls back to SwiftShader (CPU rasterization) and an animated canvas pegs a core. The flag set is copied from `engine/browser/session.py`, and the comment says to keep them in sync.

The problem is the combination, on a host with **no GPU**: `--use-angle=gl` requests the real GL driver, and `--disable-software-rasterizer` removes the fallback. On a headless server, a container, or CI, there is no GL driver — so the choice is between SwiftShader (banned by the last flag) and nothing. The documented outcome in that environment is a blank or failed render, and `web_screenshot` returns `status: ok` with a PNG that is uniformly one colour.

> [!IMPORTANT] Ledger #77
> `_gpu_args()` already takes `headless` as a parameter and returns `[]` for headed launches — the shape for a conditional is there. It needs a capability probe (or an env override) before asserting `--disable-software-rasterizer` on a machine that only has a software rasterizer. As written, the flags are correct on a workstation and wrong in the environment `web_screenshot` is most likely to be used from.

***

## 7. Smaller items

| Item | Note |
|:--|:--|
| `tempfile.mktemp(suffix=".png")` | Used in both `X11ScreenParser` and `WaylandScreenParser`. Deprecated since 3.12 for exactly the reason it is a problem: the name is generated and the file is opened later, so a local attacker can plant a symlink in between. `mkstemp` + close is the fix. |
| `scrot` return code unchecked | `run_cmd(["scrot", tmp_img])` in `screen_parse` — no `returncode` check before handing the path to tesseract. A failed capture feeds tesseract a nonexistent file. |
| `browser.close()` on a CDP connection | `web_screenshot`'s persistent branch closes the CDP-connected `Browser` in `finally`, with the comment *"disconnect only, chrome survives"*. Playwright's behaviour here is version-dependent — in some versions `close()` on a `connect_over_cdp` browser tears down the context. The comment asserts a guarantee the code does not verify. |
| `WindowsBackend.keyboard_type` | `SendKeys` also interprets `~` as Enter — any text containing a tilde submits a form. Subsumed by §4 but worth naming. |
| `screen_info` X11 | `int(w), int(h)` after `split()` with no length check — a malformed `getdisplaygeometry` output raises `ValueError` rather than returning `{"error": …}` like every sibling. |

**What is genuinely good, and should be copied:** every `WaylandBackend` method returns a shaped error rather than raising (§2); `_gpu_args` is parameterised on `headless` rather than being a constant; `_spawn_browser`'s `while … else: raise` is the correct use of the loop-`else` clause; and `web_screenshot`'s result dict reports `warnings` as a list rather than failing the whole call when navigation or a selector wait times out — so a screenshot of a half-loaded page still comes back with an explanation attached.

***

## 8. Findings

| # | Finding | Severity | Status |
|--:|:--|:--|:--|
| 75 | `run_cmd` uses `text=True` with no `errors=` — `clipboard_get` on binary clipboard data raises `UnicodeDecodeError`. `clipboard_set` also has no `timeout=`. | 🟠 medium | **not fixed** |
| 76 | `WindowsBackend.keyboard_type` sends raw text through `SendKeys`, which treats `+ ^ % ~ ( ) { } [ ]` as metacharacters — `c++` types Shift-modified characters. Silent wrong output. | 🟠 medium | **not fixed** |
| 77 | `_GPU_ARGS` combines `--use-angle=gl` with `--disable-software-rasterizer` — on a GPU-less host that yields a blank render reported as `status: ok`. | 🟠 medium | **not fixed** |
| 78 | `_spawn_browser` is check-then-act with no lock; concurrent `keep_alive` calls can both spawn, and `state.json` can end up pointing at a dead pid. `_pid_alive` also uses `os.kill(pid, 0)`, which a zombie satisfies. | 🟡 low | **not fixed** |
| 79 | `tempfile.mktemp` in both screen parsers — deprecated, TOCTOU. | 🟡 low | **not fixed** |
| 80 | `scrot` return code unchecked before tesseract; `screen_info` X11 casts to `int` without a length check. | 🔵 cosmetic | **not fixed** |
| 81 | `web_screenshot`'s `finally: browser.close()` on a CDP-connected browser asserts "disconnect only" — version-dependent, unverified. | 🟡 low | **not fixed** |

No 🔴. Three 🟠, and all three are the same shape: the code is correct on the machine it was written on (a Hyprland workstation with a GPU, a Windows desktop) and wrong on the machine it is most likely to *run* on (a headless Linux server, a container, a text field containing `+`).

***

## 9. What this changes upstream

- `documentation/README.md`: coverage tables, ledger #75–#81, priority list.
- [[29-engine-skills-handlers]] §7: `handle_computer_use` is one of the six handlers making blocking `subprocess.run` calls inside a synchronous generator. This document shows the call it blocks on can itself block indefinitely (`clipboard_set` with no timeout).

**Coverage after this batch:** Python ~60,600 → ~62,150 / 122,907 = **50.6%**. Overall ~67,000 → ~68,550 / 200,884 = **34.1%**.

**Next on the list:** `tools/online_search.py` (1,069), `engine/skills/handlers/dil_parser.py` (989), `engine/catalog.py`.
