#!/usr/bin/env python3
"""
adb_control.py — GhostChat Phone Control Script
================================================
A precise, failsafe ADB controller. Uses UIAutomator XML dumps for accurate
coordinate resolution instead of guessing from screenshots.

Usage:
    python3 adb_control.py <command> [args...]

Commands:
    dump                         Dump UI hierarchy to XML and print it
    find <text>                  Find element by text and print its center coords
    tap <x> <y>                  Tap at coordinates
    tap_text <text>              Tap element by text (dumps + finds + taps)
    tap_desc <desc>              Tap element by content-description
    tap_id <resource_id>         Tap element by resource-id (partial match OK)
    swipe <x1> <y1> <x2> <y2> [duration_ms]  Swipe between two points
    swipe_up [dist] [x]          Swipe up from center (default dist=600, x=center)
    swipe_down [dist] [x]        Swipe down from center
    swipe_left [dist] [y]        Swipe left
    swipe_right [dist] [y]       Swipe right
    input <text>                 Type text (handles spaces)
    key <keycode>                Send keycode (e.g. KEYCODE_BACK, KEYCODE_HOME)
    screenshot [local_path]      Pull screenshot to local path (default: /tmp/phone_screen.png)
    info                         Get device info (model, resolution, Android version)
    list_apps                    List installed packages
    launch <package>             Launch app by package name
    back                         Press Back key
    home                         Press Home key
    recents                      Open Recents
    lock                         Lock screen (press Power)
    unlock [pin]                 Attempt to unlock (swipe up + enter PIN if provided)
    wait <ms>                    Wait for specified milliseconds
    scroll_to <text> [max_swipes] Scroll until element with text is visible (max 5 swipes)
    check <text>                 Check if element with text exists (returns 'FOUND' or 'NOT_FOUND')
    seq <json_file>              Execute a JSON sequence file (see docs)
"""

import re
import sys
import json
import time
import subprocess
import xml.etree.ElementTree as ET
from typing import Optional


# ---------------------------------------------------------------------------
# Core ADB helpers
# ---------------------------------------------------------------------------

# Pinned device serial for this session. Set by check_device() so that every
# subsequent adb() call targets the same device even if others attach later
# or if multiple devices/emulators are present.
_DEVICE_SERIAL: Optional[str] = None


def adb(args: list[str], timeout: int = 15) -> tuple[int, str, str]:
    """Run an ADB command, return (returncode, stdout, stderr)."""
    cmd = ["adb"]
    if _DEVICE_SERIAL:
        cmd += ["-s", _DEVICE_SERIAL]
    cmd += args
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def adb_shell(cmd: str, timeout: int = 15) -> tuple[int, str, str]:
    """Run a shell command on the device."""
    return adb(["shell", cmd], timeout=timeout)


def check_device() -> bool:
    """Verify that at least one device is connected and pin a serial for this session."""
    global _DEVICE_SERIAL
    rc, out, err = adb(["devices"])
    lines = [l for l in out.splitlines()[1:] if l.strip() and "device" in l and "offline" not in l]
    if not lines:
        print("ERROR: No ADB device connected. Check USB/WiFi ADB.", file=sys.stderr)
        return False
    _DEVICE_SERIAL = lines[0].split()[0]
    if len(lines) > 1:
        print(f"WARNING: Multiple devices found — pinning this session to {_DEVICE_SERIAL}.", file=sys.stderr)
    return True


# ---------------------------------------------------------------------------
# UI Dump and Parsing
# ---------------------------------------------------------------------------

_DUMP_REMOTE = "/sdcard/window_dump.xml"
_DUMP_LOCAL  = "/tmp/adb_window_dump.xml"

# The lock-screen PIN is NOT stored here. It must be passed at runtime as
# `unlock <pin>` so no secret lives in this file.


def dump_ui(local_path: str = _DUMP_LOCAL) -> Optional[ET.Element]:
    """
    Dump the current UI hierarchy from the device and parse it.
    Returns the root ET.Element or None on failure.
    """
    # Step 1: dump on device
    rc, out, err = adb_shell(f"uiautomator dump {_DUMP_REMOTE}")
    if rc != 0 or "ERROR" in out or "ERROR" in err:
        print(f"ERROR: UI dump failed. rc={rc} out='{out}' err='{err}'", file=sys.stderr)
        return None

    # Step 2: pull to local
    rc, out, err = adb(["pull", _DUMP_REMOTE, local_path])
    if rc != 0:
        print(f"ERROR: Could not pull dump. rc={rc} err='{err}'", file=sys.stderr)
        return None

    # Step 3: clean up the remote copy so it doesn't linger between calls
    adb_shell(f"rm -f {_DUMP_REMOTE}")

    # Step 4: parse
    try:
        tree = ET.parse(local_path)
        return tree.getroot()
    except ET.ParseError as e:
        print(f"ERROR: XML parse error: {e}", file=sys.stderr)
        return None


def _parse_bounds(bounds_str: str) -> Optional[tuple[int, int, int, int]]:
    """Parse '[x1,y1][x2,y2]' into (x1, y1, x2, y2)."""
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str)
    if not m:
        return None
    return tuple(int(m.group(i)) for i in range(1, 5))


def _center(x1, y1, x2, y2) -> tuple[int, int]:
    return (x1 + x2) // 2, (y1 + y2) // 2


def _find_nodes(root: ET.Element, **attrs) -> list[ET.Element]:
    """Find all nodes matching given attributes (substring match for text/desc)."""
    results = []
    for node in root.iter("node"):
        match = True
        for key, val in attrs.items():
            node_val = node.attrib.get(key, "")
            if val.lower() not in node_val.lower():
                match = False
                break
        if match:
            results.append(node)
    return results


def _select_best_node(nodes: list[ET.Element]) -> Optional[ET.Element]:
    """
    Given multiple substring matches, discard zero-area/invalid bounds and
    prefer clickable elements over non-clickable ones (e.g. a label vs. the
    button it sits inside). Returns None if nothing usable is found.
    """
    valid = []
    for n in nodes:
        b = _parse_bounds(n.attrib.get("bounds", ""))
        if not b:
            continue
        x1, y1, x2, y2 = b
        if x2 <= x1 or y2 <= y1:
            continue
        valid.append(n)
    if not valid:
        return None
    clickable = [n for n in valid if n.attrib.get("clickable", "false") == "true"]
    return clickable[0] if clickable else valid[0]


def find_by_text(root: ET.Element, text: str) -> Optional[tuple[int, int]]:
    nodes = _find_nodes(root, text=text)
    best = _select_best_node(nodes)
    if best is None:
        return None
    bounds = _parse_bounds(best.attrib.get("bounds", ""))
    return _center(*bounds) if bounds else None


def find_by_desc(root: ET.Element, desc: str) -> Optional[tuple[int, int]]:
    nodes = _find_nodes(root, **{"content-desc": desc})
    best = _select_best_node(nodes)
    if best is None:
        return None
    bounds = _parse_bounds(best.attrib.get("bounds", ""))
    return _center(*bounds) if bounds else None


def find_by_id(root: ET.Element, res_id: str) -> Optional[tuple[int, int]]:
    nodes = _find_nodes(root, **{"resource-id": res_id})
    best = _select_best_node(nodes)
    if best is None:
        return None
    bounds = _parse_bounds(best.attrib.get("bounds", ""))
    return _center(*bounds) if bounds else None


def print_dump_summary(root: ET.Element):
    """Print a human-readable summary of visible elements."""
    rows = []
    for node in root.iter("node"):
        text    = node.attrib.get("text", "")
        desc    = node.attrib.get("content-desc", "")
        res_id  = node.attrib.get("resource-id", "")
        cls     = node.attrib.get("class", "").split(".")[-1]
        bounds  = node.attrib.get("bounds", "")
        clickable = node.attrib.get("clickable", "false") == "true"

        label = text or desc
        if not label and not res_id:
            continue

        parsed = _parse_bounds(bounds)
        if not parsed:
            continue
        cx, cy = _center(*parsed)

        rows.append({
            "text":      text,
            "desc":      desc,
            "id":        res_id,
            "class":     cls,
            "center":    f"({cx}, {cy})",
            "bounds":    bounds,
            "clickable": clickable,
        })

    print(json.dumps(rows, indent=2, ensure_ascii=False))
    return rows


# ---------------------------------------------------------------------------
# Tap verification helper
# ---------------------------------------------------------------------------

def _verify_ui_changed(root_before: ET.Element, timeout_s: float = 1.2, poll: float = 0.3) -> bool:
    """
    Poll the UI dump after an action until it differs from root_before or the
    timeout elapses. Returns True if a change was detected, False if the
    screen looks identical (a strong signal the tap missed its target).
    """
    if root_before is None:
        return False
    before_str = ET.tostring(root_before, encoding="unicode")
    end = time.time() + timeout_s
    while time.time() < end:
        time.sleep(poll)
        root_after = dump_ui()
        if root_after is None:
            continue
        after_str = ET.tostring(root_after, encoding="unicode")
        if after_str != before_str:
            return True
    return False


# ---------------------------------------------------------------------------
# Device Actions
# ---------------------------------------------------------------------------

def do_tap(x: int, y: int) -> str:
    rc, out, err = adb_shell(f"input tap {x} {y}")
    return f"Tapped ({x}, {y})" if rc == 0 else f"ERROR tapping ({x}, {y}): {err}"


def do_swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> str:
    rc, out, err = adb_shell(f"input swipe {x1} {y1} {x2} {y2} {duration_ms}")
    return f"Swiped ({x1},{y1})→({x2},{y2}) over {duration_ms}ms" if rc == 0 else f"ERROR swiping: {err}"


def do_input_text(text: str) -> str:
    # Escape special shell characters
    safe = text.replace("\\", "\\\\").replace("'", "'\\''").replace(" ", "%s")
    rc, out, err = adb_shell(f"input text '{safe}'")
    return f"Typed: {text}" if rc == 0 else f"ERROR typing: {err}"


def do_key(keycode: str) -> str:
    if not keycode.startswith("KEYCODE_"):
        keycode = f"KEYCODE_{keycode.upper()}"
    rc, out, err = adb_shell(f"input keyevent {keycode}")
    return f"Key sent: {keycode}" if rc == 0 else f"ERROR sending key {keycode}: {err}"


def do_screenshot(local_path: str = "/tmp/phone_screen.png") -> str:
    remote = "/sdcard/ghost_screen.png"
    adb_shell(f"screencap -p {remote}")
    rc, out, err = adb(["pull", remote, local_path])
    adb_shell(f"rm -f {remote}")
    if rc == 0:
        return f"Screenshot saved to {local_path}"
    return f"ERROR pulling screenshot: {err}"


def do_device_info() -> str:
    _, model, _ = adb_shell("getprop ro.product.model")
    _, brand, _ = adb_shell("getprop ro.product.brand")
    _, version, _ = adb_shell("getprop ro.build.version.release")
    _, sdk, _ = adb_shell("getprop ro.build.version.sdk")
    _, res, _ = adb_shell("wm size")
    _, density, _ = adb_shell("wm density")
    locked = is_screen_locked()
    lock_state = "locked" if locked is True else "unlocked" if locked is False else "unknown"
    info = {
        "brand": brand,
        "model": model,
        "android_version": version,
        "sdk": sdk,
        "resolution": res.replace("Physical size: ", ""),
        "density": density.replace("Physical density: ", ""),
        "lock_state": lock_state,
    }
    print(json.dumps(info, indent=2))
    return json.dumps(info)


def do_list_apps() -> str:
    _, out, _ = adb_shell("pm list packages -3")  # -3 = third party only
    packages = [l.replace("package:", "").strip() for l in out.splitlines() if l.startswith("package:")]
    print("\n".join(sorted(packages)))
    return "\n".join(packages)


def do_launch(package: str) -> str:
    rc, out, err = adb_shell(
        f"monkey -p {package} -c android.intent.category.LAUNCHER 1"
    )
    if rc == 0:
        return f"Launched {package}"

    # Fallback: some ROMs (custom MediaTek skins, etc.) block monkey unless
    # "USB debugging (Security settings)" is separately enabled. Resolve the
    # launcher activity directly and start it with am instead.
    _, resolved, _ = adb_shell(f"cmd package resolve-activity --brief {package}")
    lines = [l.strip() for l in resolved.splitlines() if l.strip()]
    component = lines[-1] if lines else ""
    if component and "/" in component:
        rc2, out2, err2 = adb_shell(f"am start -n {component}")
        if rc2 == 0:
            return f"Launched {package} via am start -n {component} (monkey fallback)"
        return f"ERROR launching {package}: monkey failed ({err}); am start fallback failed ({err2})"

    return f"ERROR launching {package}: {err}"


def get_screen_center() -> tuple[int, int]:
    _, out, _ = adb_shell("wm size")
    m = re.search(r"(\d+)x(\d+)", out)
    if m:
        return int(m.group(1)) // 2, int(m.group(2)) // 2
    return 540, 960  # sane default


def is_screen_locked() -> Optional[bool]:
    """
    Best-effort keyguard check. Returns True if locked, False if unlocked,
    or None if it couldn't be determined on this device/Android version.

    Primary signal: `dumpsys window policy` -> KeyguardServiceDelegate's
    `showing=` (and the nested KeyguardStateMonitor's `mIsShowing=`), which
    is what's actually present and confirmed flipping true/false on this
    device. Many docs/examples reference `isStatusBarKeyguard=`, but that
    key doesn't exist on all ROMs (including MediaTek-skinned builds) — it's
    kept as a secondary check in case it's present on other devices.
    """
    _, out, _ = adb_shell("dumpsys window policy")

    m = re.search(r"\bshowing=(true|false)", out)
    if m:
        return m.group(1) == "true"

    m = re.search(r"\bmIsShowing=(true|false)", out)
    if m:
        return m.group(1) == "true"

    flat = out.lower().replace(" ", "")
    if "isstatusbarkeyguard=true" in flat:
        return True
    if "isstatusbarkeyguard=false" in flat:
        return False

    # Fallback for devices/ROMs where the policy dump exposes none of the above
    _, out2, _ = adb_shell("dumpsys trust")
    low2 = out2.lower()
    m2 = re.search(r"devicelocked=(\d+|true|false)", low2)
    if m2:
        return m2.group(1) in ("1", "true")
    if "keyguard is not showing" in low2:
        return False
    return None


def do_unlock(pin: Optional[str] = None) -> str:
    """
    Check whether the device is actually locked; if so, wake it, swipe up,
    and if a PIN field appears, type the provided pin and press Enter.
    No-ops if the device is already unlocked. If a PIN is required but none
    was supplied, returns an error prompting the caller to provide one.
    """
    locked = is_screen_locked()
    if locked is False:
        return "Device already unlocked — no action taken."

    do_key("KEYCODE_WAKEUP")
    time.sleep(0.4)
    cx, cy = get_screen_center()
    swipe_result = do_swipe(cx, cy + 600, cx, cy - 600, 300)
    time.sleep(0.5)

    # See if a PIN/password field is now on screen
    root = dump_ui()
    needs_pin = False
    if root is not None:
        for node in root.iter("node"):
            cls = node.attrib.get("class", "")
            rid = node.attrib.get("resource-id", "").lower()
            if "edittext" in cls.lower() or "pin" in rid or "password" in rid:
                needs_pin = True
                break

    if not needs_pin:
        still_locked = is_screen_locked()
        if still_locked is False:
            return f"Swipe-up unlocked the device (no PIN required). {swipe_result}"
        # Unclear state — fall through; a PIN is attempted only if provided.

    if not pin:
        return (f"ERROR: Device is locked and requires a PIN/password, but none was "
                f"provided. Ask the user for the unlock PIN, then re-run: unlock <pin>. "
                f"({swipe_result})")

    type_result = do_input_text(pin)
    time.sleep(0.2)
    enter_result = do_key("KEYCODE_ENTER")
    time.sleep(0.4)

    final_locked = is_screen_locked()
    if final_locked is False:
        return f"Unlocked via PIN entry. ({swipe_result}; {type_result}; {enter_result})"
    if final_locked is True:
        return f"WARNING: unlock sequence ran but device still appears locked. ({swipe_result}; {type_result}; {enter_result})"
    return f"Unlock sequence ran; lock state could not be confirmed on this device. ({swipe_result}; {type_result}; {enter_result})"


# ---------------------------------------------------------------------------
# High-level compound actions
# ---------------------------------------------------------------------------

def do_tap_text(text: str) -> str:
    root = dump_ui()
    if root is None:
        return "ERROR: UI dump failed — cannot resolve coordinates for text tap."
    coords = find_by_text(root, text)
    if coords is None:
        return f"ERROR: Element with text '{text}' not found in UI dump."
    x, y = coords
    result = do_tap(x, y)
    changed = _verify_ui_changed(root)
    status = "UI change detected" if changed else "WARNING: no UI change detected after tap"
    return f"Resolved '{text}' → ({x}, {y}). {result} [{status}]"


def do_tap_desc(desc: str) -> str:
    root = dump_ui()
    if root is None:
        return "ERROR: UI dump failed — cannot resolve coordinates for content-desc tap."
    coords = find_by_desc(root, desc)
    if coords is None:
        return f"ERROR: Element with content-desc '{desc}' not found in UI dump."
    x, y = coords
    result = do_tap(x, y)
    changed = _verify_ui_changed(root)
    status = "UI change detected" if changed else "WARNING: no UI change detected after tap"
    return f"Resolved desc='{desc}' → ({x}, {y}). {result} [{status}]"


def do_tap_id(res_id: str) -> str:
    root = dump_ui()
    if root is None:
        return "ERROR: UI dump failed — cannot resolve coordinates for resource-id tap."
    coords = find_by_id(root, res_id)
    if coords is None:
        return f"ERROR: Element with resource-id '{res_id}' not found in UI dump."
    x, y = coords
    result = do_tap(x, y)
    changed = _verify_ui_changed(root)
    status = "UI change detected" if changed else "WARNING: no UI change detected after tap"
    return f"Resolved id='{res_id}' → ({x}, {y}). {result} [{status}]"


def do_scroll_to(text: str, max_swipes: int = 5) -> str:
    cx, cy = get_screen_center()
    for i in range(max_swipes):
        root = dump_ui()
        if root is None:
            return "ERROR: UI dump failed during scroll."
        coords = find_by_text(root, text)
        if coords:
            return f"FOUND '{text}' after {i} swipes at {coords}"
        # Swipe up to scroll down
        do_swipe(cx, cy + 300, cx, cy - 300, 400)
        time.sleep(0.5)
    return f"NOT_FOUND: '{text}' not visible after {max_swipes} swipes."


def do_check(text: str) -> str:
    root = dump_ui()
    if root is None:
        return "ERROR: UI dump failed."
    coords = find_by_text(root, text)
    result = "FOUND" if coords else "NOT_FOUND"
    print(result)
    return result


def do_swipe_direction(direction: str, dist: int = 600, axis: Optional[int] = None) -> str:
    cx, cy = get_screen_center()
    if direction == "up":
        x = axis if axis is not None else cx
        return do_swipe(x, cy + dist // 2, x, cy - dist // 2, 400)
    elif direction == "down":
        x = axis if axis is not None else cx
        return do_swipe(x, cy - dist // 2, x, cy + dist // 2, 400)
    elif direction == "left":
        y = axis if axis is not None else cy
        return do_swipe(cx + dist // 2, y, cx - dist // 2, y, 400)
    elif direction == "right":
        y = axis if axis is not None else cy
        return do_swipe(cx - dist // 2, y, cx + dist // 2, y, 400)
    return f"ERROR: Unknown direction '{direction}'"


def do_sequence(json_file: str) -> str:
    """
    Execute a JSON sequence file.
    Format: list of {"cmd": "<command>", "args": [...], "wait_ms": 500}
    """
    try:
        with open(json_file) as f:
            steps = json.load(f)
    except Exception as e:
        return f"ERROR loading sequence file: {e}"

    results = []
    for i, step in enumerate(steps):
        cmd   = step.get("cmd", "")
        args  = step.get("args", [])
        wait  = step.get("wait_ms", 0)

        print(f"[Step {i+1}/{len(steps)}] {cmd} {args}", file=sys.stderr)
        result = _dispatch(cmd, args)
        results.append({"step": i + 1, "cmd": cmd, "result": result})
        print(f"  → {result}", file=sys.stderr)

        if wait > 0:
            time.sleep(wait / 1000.0)

    print(json.dumps(results, indent=2))
    return json.dumps(results)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def _dispatch(cmd: str, args: list[str]) -> str:
    try:
        if cmd == "dump":
            root = dump_ui()
            if root is None:
                return "ERROR: UI dump failed."
            print_dump_summary(root)
            return "Dump complete."

        elif cmd == "find":
            if not args:
                return "ERROR: find requires <text>"
            root = dump_ui()
            if root is None:
                return "ERROR: UI dump failed."
            coords = find_by_text(root, " ".join(args))
            if coords:
                print(f"{coords[0]} {coords[1]}")
                return f"Found at ({coords[0]}, {coords[1]})"
            return "NOT_FOUND"

        elif cmd == "tap":
            if len(args) < 2:
                return "ERROR: tap requires <x> <y>"
            return do_tap(int(args[0]), int(args[1]))

        elif cmd == "tap_text":
            if not args:
                return "ERROR: tap_text requires <text>"
            return do_tap_text(" ".join(args))

        elif cmd == "tap_desc":
            if not args:
                return "ERROR: tap_desc requires <desc>"
            return do_tap_desc(" ".join(args))

        elif cmd == "tap_id":
            if not args:
                return "ERROR: tap_id requires <resource_id>"
            return do_tap_id(" ".join(args))

        elif cmd == "swipe":
            if len(args) < 4:
                return "ERROR: swipe requires <x1> <y1> <x2> <y2> [duration_ms]"
            dur = int(args[4]) if len(args) > 4 else 300
            return do_swipe(int(args[0]), int(args[1]), int(args[2]), int(args[3]), dur)

        elif cmd == "swipe_up":
            dist = int(args[0]) if args else 600
            axis = int(args[1]) if len(args) > 1 else None
            return do_swipe_direction("up", dist, axis)

        elif cmd == "swipe_down":
            dist = int(args[0]) if args else 600
            axis = int(args[1]) if len(args) > 1 else None
            return do_swipe_direction("down", dist, axis)

        elif cmd == "swipe_left":
            dist = int(args[0]) if args else 600
            axis = int(args[1]) if len(args) > 1 else None
            return do_swipe_direction("left", dist, axis)

        elif cmd == "swipe_right":
            dist = int(args[0]) if args else 600
            axis = int(args[1]) if len(args) > 1 else None
            return do_swipe_direction("right", dist, axis)

        elif cmd == "input":
            if not args:
                return "ERROR: input requires <text>"
            return do_input_text(" ".join(args))

        elif cmd == "key":
            if not args:
                return "ERROR: key requires <keycode>"
            return do_key(args[0])

        elif cmd == "screenshot":
            path = args[0] if args else "/tmp/phone_screen.png"
            return do_screenshot(path)

        elif cmd == "info":
            return do_device_info()

        elif cmd == "list_apps":
            return do_list_apps()

        elif cmd == "launch":
            if not args:
                return "ERROR: launch requires <package>"
            return do_launch(args[0])

        elif cmd == "back":
            return do_key("KEYCODE_BACK")

        elif cmd == "home":
            return do_key("KEYCODE_HOME")

        elif cmd == "recents":
            return do_key("KEYCODE_APP_SWITCH")

        elif cmd == "lock":
            return do_key("KEYCODE_POWER")

        elif cmd == "unlock":
            pin = args[0] if args else None
            return do_unlock(pin)

        elif cmd == "wait":
            ms = int(args[0]) if args else 1000
            time.sleep(ms / 1000.0)
            return f"Waited {ms}ms"

        elif cmd == "scroll_to":
            if not args:
                return "ERROR: scroll_to requires <text>"
            max_swipes = int(args[-1]) if len(args) > 1 and args[-1].isdigit() else 5
            text = " ".join(args[:-1]) if len(args) > 1 and args[-1].isdigit() else " ".join(args)
            return do_scroll_to(text, max_swipes)

        elif cmd == "check":
            if not args:
                return "ERROR: check requires <text>"
            return do_check(" ".join(args))

        elif cmd == "seq":
            if not args:
                return "ERROR: seq requires <json_file>"
            return do_sequence(args[0])

        else:
            return f"ERROR: Unknown command '{cmd}'. Run with --help for usage."

    except Exception as e:
        return f"EXCEPTION in '{cmd}': {type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    if not check_device():
        sys.exit(1)

    cmd  = sys.argv[1]
    args = sys.argv[2:]

    result = _dispatch(cmd, args)

    # Print result if it wasn't already printed by the command
    if result and not result.startswith("{") and not result.startswith("["):
        print(result)


if __name__ == "__main__":
    main()