# Phone Control: ADB Guardian

the user's hands-on phone controller. Uses UIAutomator XML dumps for pixel-perfect
coordinate resolution — never guesses from screenshots. Every tap is verified
against a before/after UI diff. Every action is atomic. Runs via
`adb_control.py` through ``.

---

## Trigger Guard

| Condition | Action |
|---|---|
| "open [app] on my phone" | Use `launch` |
| "tap [element]" or "click [something]" on phone | Use `tap_text` or `tap_id` |
| "scroll down / swipe" on phone | Use `swipe_up` / `swipe_down` |
| "type [text]" on phone | Use `input` |
| "take a screenshot of my phone" | Use `screenshot` |
| "go back / press home" on phone | Use `back` / `home` |
| "unlock my phone" / "is my phone locked" | Use `unlock` |
| Request names 2+ device actions (tap, then wait, then dump, then type, etc.), whether or not it says "automate" | **Build a `seq` JSON file, use `seq` — do not issue separate `execute_command` calls per step** |
| Phone not connected or ADB error | Run `info` first. Fix connection before any action. |

**This is the most commonly missed rule:** any time a request chains multiple
device actions — even informally phrased like "tap the search bar, wait a
sec, then type the user and hit the first result" — that is a `seq` job. The
literal word "automate" does not need to appear. The test is: does this
request involve 2 or more taps/waits/types/dumps in sequence? If yes, write
the JSON file and call `seq` once, rather than emitting one `execute_command`
per step. Falling back to individual calls for a multi-step request is a
deviation from this skill, not a valid alternative — it's slower (one
subprocess spin-up per step) and loses the atomicity `seq` gives you.

---

## Script Path

```
PROJECT_ROOT/skills/phone_control/scripts/adb_control.py
```

Always call it as:
```bash
python3 PROJECT_ROOT/skills/phone_control/scripts/adb_control.py <command> [args...]
```

---

## Step 0 — Connection Check (always first)

Before any phone action, verify the device is reachable:

```bash
python3 PROJECT_ROOT/skills/phone_control/scripts/adb_control.py info
```

| Result | Action |
|---|---|
| Returns JSON with model/resolution and `"lock_state": "unlocked"` | Proceed — device is connected and unlocked |
| Returns JSON with model/resolution and `"lock_state": "locked"` | Run `unlock` before any `tap`/`dump`/`launch` steps — a locked screen will only ever show the lock screen UI |
| Returns JSON with `"lock_state": "unknown"` | This ROM doesn't expose the keyguard flag cleanly. Treat as possibly locked — run `dump` and check whether normal UI or a lock screen is visible before proceeding |
| Returns `ERROR: No ADB device connected` | Stop. Tell the user to: (1) plug in USB and enable USB debugging, OR (2) pair via WiFi ADB (`adb pair`) |
| Any other error | Stop. Report exact error. Do not proceed blindly. |

**Note on multiple devices:** the script auto-pins itself to the first device
`adb devices` reports and reuses that serial for every subsequent call in the
run. You don't need to pass a serial — but if the user has more than one
device/emulator attached and means a different one, ask before proceeding, since
the script will silently pick the first it sees.

---

## Core Commands Reference

### 🔍 UI Inspection

**Dump full UI hierarchy** — always use before tapping if unsure what's on screen:
```bash
python3 PROJECT_ROOT/skills/phone_control/scripts/adb_control.py dump
```
Returns a JSON array of all visible elements with `text`, `desc`, `id`, `class`, `center`, `bounds`, and `clickable`.

**Find element center by text** (case-insensitive, substring match):
```bash
python3 PROJECT_ROOT/skills/phone_control/scripts/adb_control.py find Settings
```
Returns `x y` coordinates or `NOT_FOUND`.

**Check if element exists** (returns `FOUND` or `NOT_FOUND`):
```bash
python3 PROJECT_ROOT/skills/phone_control/scripts/adb_control.py check Allow
```

---

### 👆 Tapping

**Tap by text** (dump → find → tap in one call — preferred method):
```bash
python3 PROJECT_ROOT/skills/phone_control/scripts/adb_control.py tap_text Settings
```

**Tap by content-description** (for icon buttons without visible text):
```bash
python3 PROJECT_ROOT/skills/phone_control/scripts/adb_control.py tap_desc "Navigate up"
```

**Tap by resource-id** (partial match — most stable across app versions):
```bash
python3 PROJECT_ROOT/skills/phone_control/scripts/adb_control.py tap_id com.whatsapp:id/send_button
```

**Tap raw coordinates** (use ONLY when dump confirmed the coords):
```bash
python3 PROJECT_ROOT/skills/phone_control/scripts/adb_control.py tap 540 960
```

**Reading tap results:** `tap_text`, `tap_desc`, and `tap_id` all end their
output with a verification tag:
- `[UI change detected]` — the tap registered and the screen actually changed. Safe to proceed.
- `[WARNING: no UI change detected after tap]` — the tap fired but nothing on
  screen changed within ~1.2s. Treat this as a probable miss: run `dump`,
  re-check the target is still the right element (multiple matches can
  resolve to the wrong node), and retry rather than assuming the step worked.

Also note: when a text/desc/id substring matches more than one element, the
script now prefers `clickable="true"` nodes over non-clickable ones (e.g. a
label sitting inside a button) and discards any zero-area bounds — so
`tap_text` picking the "wrong" match is rarer, but still check the resolved
coordinates in the returned string if a tap looks off.

---

### 🔄 Swiping / Scrolling

**Directional swipes** (auto-centers on screen):
```bash
python3 PROJECT_ROOT/skills/phone_control/scripts/adb_control.py swipe_up
python3 PROJECT_ROOT/skills/phone_control/scripts/adb_control.py swipe_down
python3 PROJECT_ROOT/skills/phone_control/scripts/adb_control.py swipe_left
python3 PROJECT_ROOT/skills/phone_control/scripts/adb_control.py swipe_right
```
Optional args: `[distance_px] [axis_px]`
```bash
python3 PROJECT_ROOT/skills/phone_control/scripts/adb_control.py swipe_up 800 540
```

**Custom swipe** (explicit coordinates):
```bash
python3 PROJECT_ROOT/skills/phone_control/scripts/adb_control.py swipe 540 1200 540 400 500
```
Args: `x1 y1 x2 y2 [duration_ms]`

**Scroll until element is visible** (auto-swipes up to N times):
```bash
python3 PROJECT_ROOT/skills/phone_control/scripts/adb_control.py scroll_to "Privacy Policy" 8
```

---

### ⌨️ Typing & Keys

**Type text** (handles spaces; ASCII only — `input text` under the hood can't
type Bangla or other non-ASCII characters, and is slow character-by-character
for long strings):
```bash
python3 PROJECT_ROOT/skills/phone_control/scripts/adb_control.py input Hello World
```

**Send hardware keys**:
```bash
python3 PROJECT_ROOT/skills/phone_control/scripts/adb_control.py back
python3 PROJECT_ROOT/skills/phone_control/scripts/adb_control.py home
python3 PROJECT_ROOT/skills/phone_control/scripts/adb_control.py recents
python3 PROJECT_ROOT/skills/phone_control/scripts/adb_control.py lock
```

**Custom keycode**:
```bash
python3 PROJECT_ROOT/skills/phone_control/scripts/adb_control.py key KEYCODE_ENTER
python3 PROJECT_ROOT/skills/phone_control/scripts/adb_control.py key KEYCODE_DEL
```

---

### 🔓 Unlock

Check lock state anytime via `info`'s `lock_state` field (`locked` /
`unlocked` / `unknown`), or run `unlock` directly — it does its own internal
lock check and no-ops if already unlocked:

```bash
python3 PROJECT_ROOT/skills/phone_control/scripts/adb_control.py unlock [pin]
```

Behavior:
1. Checks lock state first via `dumpsys` (`isStatusBarKeyguard` / `dumpsys trust`). If already unlocked, it's a no-op — `"Device already unlocked — no action taken."`
2. If locked: wakes the screen, swipes up, then dumps the UI to see if a PIN/password field appeared.
3. If a PIN field shows (or the state is ambiguous) and a `pin` argument was supplied, types it and presses Enter.
4. If a PIN is required but **no `pin` was provided**, the script returns an error telling you to supply one — in that case, ask the user for their unlock PIN/password, then re-run `unlock <pin>`.
5. Re-checks lock state and reports one of: unlocked via swipe, unlocked via PIN, still locked (warning), or state could not be confirmed (some ROMs don't expose the keyguard flag — treat this as "probably fine, verify with `dump` before proceeding" rather than a hard failure).

The PIN is **never stored** in the script — it is passed at runtime as
`unlock <pin>`. If you don't already know it (e.g. from the user earlier in
the session) and the device is locked, ask the user for the pass before
retrying. Never print the PIN into chat, logs, or sequence files.

If `unlock` reports still-locked or unconfirmed twice in a row, stop and
report to the user rather than retrying indefinitely — repeated failed PIN
attempts can trigger Android's lockout/backup-PIN screen.

---

### 📸 Screenshot

```bash
python3 PROJECT_ROOT/skills/phone_control/scripts/adb_control.py screenshot /tmp/phone_screen.png
```

After taking a screenshot, upload it to model context if you need to show the user:
```bash
/tmp/phone_screen.png
```

---

### 📱 App Management

**List installed apps**:
```bash
python3 PROJECT_ROOT/skills/phone_control/scripts/adb_control.py list_apps
```

**Launch app by package name**:
```bash
python3 PROJECT_ROOT/skills/phone_control/scripts/adb_control.py launch com.whatsapp
```

If the primary launch method (`monkey`) is blocked by the ROM — common on
some MediaTek/custom-skin devices unless "USB debugging (Security settings)"
is separately enabled — the script automatically falls back to resolving the
launcher activity and starting it directly with `am start -n`. You don't need
to do anything extra; just check the returned string, which will say which
path succeeded (or report both errors if neither worked).

Common package names:
| App | Package |
|---|---|
| WhatsApp | `com.whatsapp` |
| Telegram | `org.telegram.messenger` |
| YouTube | `com.google.android.youtube` |
| Chrome | `com.android.chrome` |
| Settings | `com.android.settings` |
| Camera | `com.android.camera2` |
| Gallery | `com.google.android.apps.photos` |
| Maps | `com.google.android.apps.maps` |
| Play Store | `com.android.vending` |

If unsure of the package name, run `list_apps` and grep for partial match.

---

### ⏱️ Waiting

```bash
python3 PROJECT_ROOT/skills/phone_control/scripts/adb_control.py wait 1500
```
Waits 1500ms. Use between actions when app needs time to load/animate.

---

## Automation Sequences (Multi-Step Tasks)

For complex phone automations (3+ steps), build a **JSON sequence file** instead of
running individual commands. This is more reliable, readable, and self-documenting.

### Sequence File Format

```json
[
  {"cmd": "launch",    "args": ["com.whatsapp"],  "wait_ms": 2000},
  {"cmd": "tap_text",  "args": ["Search..."],     "wait_ms": 500},
  {"cmd": "input",     "args": ["the user"],         "wait_ms": 800},
  {"cmd": "tap_text",  "args": ["the user"],         "wait_ms": 1000},
  {"cmd": "tap_text",  "args": ["Type a message"],"wait_ms": 300},
  {"cmd": "input",     "args": ["Hey 👋"],        "wait_ms": 300},
  {"cmd": "tap_id",    "args": ["send_button"],   "wait_ms": 500},
  {"cmd": "screenshot","args": ["/tmp/sent.png"], "wait_ms": 0}
]
```

**Fields:**
| Field | Type | Description |
|---|---|---|
| `cmd` | string | Any command from this instruction |
| `args` | array | Command arguments as strings |
| `wait_ms` | int | Milliseconds to wait AFTER this step |

If the device might be locked at the start of a sequence, lead with an
`unlock` step before `launch`/`tap_*` steps — a locked screen means `dump`
will only ever see the lock-screen UI, and every step after it will fail with
`NOT_FOUND`.

**Write the sequence file**, then execute it:
```bash
cat > /tmp/phone_seq.json << 'EOF'
[
  {"cmd": "unlock",    "args": [],                "wait_ms": 500},
  {"cmd": "home",      "args": [],                "wait_ms": 500},
  {"cmd": "launch",    "args": ["com.whatsapp"],  "wait_ms": 2500},
  {"cmd": "tap_text",  "args": ["Search..."],     "wait_ms": 600}
]
EOF
python3 PROJECT_ROOT/skills/phone_control/scripts/adb_control.py seq /tmp/phone_seq.json
```

**Reading `seq` output:** the command returns a JSON array of
`{"step": N, "cmd": ..., "result": ...}` objects. Scan every `result` string
for `ERROR`, `NOT_FOUND`, or `WARNING: no UI change detected` before telling
the user the automation succeeded — a sequence can run to completion with
several silently-failed steps in the middle if you only check the final exit
code.

---

## Decision Table: Which Tap Method to Use

| Situation | Use |
|---|---|
| Element has visible label text | `tap_text` |
| Icon or button with no text (e.g., back arrow, send icon) | `tap_desc` |
| Button identified by code (resource-id visible in dump) | `tap_id` |
| Coordinates already confirmed from dump output | `tap` |
| **Never** | Raw coords from screenshot guessing |

### Coordinate Resolution Protocol (when uncertain)

1. Run `dump` to get full UI state
2. Read the JSON output — find the element by `text`, `desc`, or `id`
3. Use the `center` coordinates shown in the dump output if tapping directly
4. OR just use `tap_text`/`tap_id` which resolve coordinates internally and
   verify the tap landed via the `[UI change detected]` tag

---

## Error Handling

| Error | Cause | Fix |
|---|---|---|
| `ERROR: No ADB device connected` | USB not plugged / USB debugging off / WiFi ADB not enabled | Check connection, enable USB debugging in Developer Options |
| `ERROR: UI dump failed` | Screen is off, secure input is open, or app is animating | Try `wait 500` then retry dump. If the screen is off/locked, run `unlock` first. |
| `NOT_FOUND: element` | Element is not on screen or text mismatch | Run `dump` first to see what's actually visible |
| `ERROR: Could not pull dump` | Storage permission or path issue on device | Check device storage / remount |
| `XML parse error` | Corrupt dump (happens during heavy animations) | Retry the dump after a short `wait` |
| `WARNING: no UI change detected after tap` | Tap coordinates resolved to the wrong node, or the screen needs longer to react | Run `dump`, confirm the target element/bounds, retry — don't just re-tap blindly |
| Tap hits wrong element | Multiple elements with same text, and none marked `clickable` | Use `tap_id` with resource-id for precision |
| `unlock` reports still-locked or unconfirmed | Wrong PIN, non-standard lock screen (pattern/biometric-only), or ROM doesn't expose keyguard state | Stop after 1 retry and report to the user — do not hammer the PIN entry |

---

## Compound Workflows

### Open an app and interact
```bash
python3 PROJECT_ROOT/skills/phone_control/scripts/adb_control.py launch com.android.settings
```
Wait for launch, then:
```bash
python3 PROJECT_ROOT/skills/phone_control/scripts/adb_control.py wait 1500 && python3 PROJECT_ROOT/skills/phone_control/scripts/adb_control.py tap_text "Wi-Fi"
```

### Verify action succeeded
After any important tap, either read the `[UI change detected]` /
`[WARNING...]` tag already appended to the `tap_text`/`tap_desc`/`tap_id`
result, or for extra certainty:
```bash
python3 PROJECT_ROOT/skills/phone_control/scripts/adb_control.py dump
```
Read the dump output. Confirm the expected next screen elements are visible.

### Scroll and find
```bash
python3 PROJECT_ROOT/skills/phone_control/scripts/adb_control.py scroll_to "About phone" 6
```

### Unlock then act
```bash
python3 PROJECT_ROOT/skills/phone_control/scripts/adb_control.py unlock [pin]
```
Check the result string for `still appears locked` before proceeding — don't
chain further taps onto a lock screen.

---

## Master Rules

1. **Always check device first.** `info` must succeed before any action.
2. **Never guess coordinates from screenshots.** Use `dump` → `tap_text` / `tap_id`.
3. **Use `seq` for 2+ chained device actions — this applies regardless of phrasing.** A JSON seq file is more reliable and faster than chaining individual `execute_command` calls manually. Lead with `unlock` if the device might be locked.
4. **Wait after every app launch.** Give at least 1500–2500ms for app to load before interacting.
5. **Verify with dump (or the built-in tap-verification tag) after critical actions.** Don't assume a tap worked — confirm the new screen state.
6. **Handle NOT_FOUND gracefully.** If `tap_text` fails, run `dump` to see what's actually on screen, then re-plan.
7. **One action, one step.** Never try to combine multiple UI actions in a single `execute_command` unless using `&&` for truly atomic sequences.
8. **Report coordinates used.** Always state which element was resolved and at which coordinates — no silent guesses.
9. **Never surface the unlock PIN.** It's passed at runtime as an argument — don't echo it into chat, logs, or sequence files.
10. **Don't retry `unlock` more than once on failure.** Repeated wrong-PIN attempts risk tripping Android's lockout screen. Stop and report instead.