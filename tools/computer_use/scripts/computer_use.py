#!/usr/bin/env python3
"""
computer_use.py — Cross-platform computer control backend for Sable.

Supports:
  - Linux X11  (xdotool, scrot/maim, xclip, xwininfo)
  - Linux Wayland (grim, slurp, wl-clipboard, ydotool/wtype, hyprctl)
  - Windows    (PowerShell + .NET System.Windows.Forms / User32)

Usage:
  python computer_use.py '{"action": "screenshot", "fullscreen": true}'
  python computer_use.py '{"action": "mouse_click", "x": 500, "y": 300, "button": "left"}'
  python computer_use.py '{"action": "keyboard_type", "text": "hello world"}'
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


# ─── Platform Detection ───────────────────────────────────────────────────────

def detect_platform() -> dict[str, str]:
    """Detect OS and display server."""
    os_name = platform.system().lower()  # 'linux', 'windows', 'darwin'
    session = os.environ.get("XDG_SESSION_TYPE", "").lower()
    wayland_display = os.environ.get("WAYLAND_DISPLAY", "")
    display = os.environ.get("DISPLAY", "")

    if os_name == "linux":
        if session == "wayland" or wayland_display:
            # Check if it's Hyprland specifically
            if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
                return {"os": "linux", "display": "wayland", "compositor": "hyprland"}
            return {"os": "linux", "display": "wayland", "compositor": "generic"}
        elif display:
            return {"os": "linux", "display": "x11", "compositor": "x11"}
        else:
            # Fallback: assume X11
            return {"os": "linux", "display": "x11", "compositor": "x11"}
    elif os_name == "windows":
        return {"os": "windows", "display": "win32", "compositor": "dwm"}
    else:
        return {"os": os_name, "display": "unknown", "compositor": "unknown"}


PLATFORM = detect_platform()


def run_cmd(cmd: list[str] | str, shell: bool = False, timeout: int = 15) -> subprocess.CompletedProcess:
    """Run a command, return CompletedProcess."""
    return subprocess.run(
        cmd, shell=shell, capture_output=True, text=True, timeout=timeout
    )


def run_powershell(script: str, timeout: int = 15) -> subprocess.CompletedProcess:
    """Run a PowerShell script on Windows."""
    return run_cmd(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        timeout=timeout,
    )


def which(binary: str) -> bool:
    return shutil.which(binary) is not None


def run_checked(cmd: list[str], timeout: int = 15) -> tuple[bool, str]:
    """Run command, return (success, error_message). Never swallow failures."""
    r = run_cmd(cmd, timeout=timeout)
    if r.returncode == 0:
        return True, ""
    return False, (r.stderr or r.stdout).strip()


# ydotool 1.x button encoding: low bits = button index, 0x40 = down, 0x80 = up
YDO_BTN = {"left": 0x00, "right": 0x01, "middle": 0x02}

MOD_KEYS = {"ctrl", "control", "shift", "alt", "super", "meta", "win"}

# Raw linux input-event-codes for ydotool key (layout-independent physical codes)
KEYCODES: dict[str, int] = {
    "ctrl": 29, "control": 29, "shift": 42, "alt": 56, "super": 125, "meta": 125, "win": 125,
    "a": 30, "b": 48, "c": 46, "d": 32, "e": 18, "f": 33, "g": 34, "h": 35, "i": 23,
    "j": 36, "k": 37, "l": 38, "m": 50, "n": 49, "o": 24, "p": 25, "q": 16, "r": 19,
    "s": 31, "t": 20, "u": 22, "v": 47, "w": 17, "x": 45, "y": 21, "z": 44,
    "0": 11, "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9, "9": 10,
    "return": 28, "enter": 28, "tab": 15, "escape": 1, "esc": 1, "space": 57,
    "backspace": 14, "delete": 111, "del": 111, "insert": 110,
    "home": 102, "end": 107, "pageup": 104, "pagedown": 109,
    "up": 103, "down": 108, "left": 105, "right": 106,
    "f1": 59, "f2": 60, "f3": 61, "f4": 62, "f5": 63, "f6": 64,
    "f7": 65, "f8": 66, "f9": 67, "f10": 68, "f11": 87, "f12": 88,
}


# ─── Output Path ──────────────────────────────────────────────────────────────

def default_output_dir() -> Path:
    """Get default output directory for screenshots."""
    out = Path.home() / "sable_output" / "assets"
    out.mkdir(parents=True, exist_ok=True)
    return out


# ─── Linux X11 Backend ───────────────────────────────────────────────────────

class X11Backend:
    """X11 control via xdotool, scrot, xclip, xwininfo."""

    def screenshot(self, params: dict) -> dict:
        save_path = params.get("save_path") or str(
            default_output_dir() / f"screenshot_{int(time.time())}.png"
        )
        if params.get("fullscreen", True) and not all(
            params.get(k) is not None for k in ("x", "y", "x2", "y2")
        ):
            # Full screen
            if which("scrot"):
                run_cmd(["scrot", save_path])
            elif which("maim"):
                run_cmd(["maim", save_path])
            elif which("import"):
                run_cmd(["import", "-window", "root", save_path])
            else:
                return {"error": "No screenshot tool found (need scrot/maim/import)"}
        else:
            # Region
            x, y = params["x"], params["y"]
            w = params["x2"] - x if params.get("x2") else 100
            h = params["y2"] - y if params.get("y2") else 100
            if which("maim"):
                run_cmd(["maim", "-g", f"{w}x{h}+{x}+{y}", save_path])
            elif which("scrot"):
                run_cmd(["scrot", "-a", f"{x},{y},{w},{h}", save_path])
            elif which("import"):
                run_cmd(["import", "-window", "root", "-crop", f"{w}x{h}+{x}+{y}", save_path])
            else:
                return {"error": "No screenshot tool found"}
        return {"path": save_path, "status": "ok"}

    def mouse_click(self, params: dict) -> dict:
        x, y = params.get("x"), params.get("y")
        button_map = {"left": "1", "middle": "2", "right": "3"}
        btn = button_map.get(params.get("button", "left"), "1")
        clicks = params.get("clicks", 1)

        cmd = []
        if x is not None and y is not None:
            cmd += ["xdotool", "mousemove", str(x), str(y)]
            if clicks > 1:
                cmd += ["click", "--repeat", str(clicks), btn]
            else:
                cmd += ["click", btn]
        else:
            cmd = ["xdotool", "click", btn]
            if clicks > 1:
                cmd = ["xdotool", "click", "--repeat", str(clicks), btn]

        run_cmd(cmd)
        return {"status": "ok", "action": "mouse_click"}

    def mouse_move(self, params: dict) -> dict:
        run_cmd(["xdotool", "mousemove", str(params["x"]), str(params["y"])])
        return {"status": "ok"}

    def mouse_drag(self, params: dict) -> dict:
        x, y = params["x"], params["y"]
        x2, y2 = params["x2"], params["y2"]
        run_cmd(["xdotool", "mousemove", str(x), str(y), "mousedown", "1",
                 "mousemove", str(x2), str(y2), "mouseup", "1"])
        return {"status": "ok"}

    def mouse_scroll(self, params: dict) -> dict:
        direction = params.get("direction", "down")
        amount = params.get("amount", 3)
        # xdotool: 4=up, 5=down, 6=left, 7=right
        btn_map = {"up": "4", "down": "5", "left": "6", "right": "7"}
        btn = btn_map.get(direction, "5")
        run_cmd(["xdotool", "click", "--repeat", str(amount), btn])
        return {"status": "ok"}

    def keyboard_type(self, params: dict) -> dict:
        text = params.get("text", "")
        run_cmd(["xdotool", "type", "--clearmodifiers", text])
        return {"status": "ok"}

    def keyboard_key(self, params: dict) -> dict:
        keys = params.get("keys", "")
        # xdotool uses lowercase: ctrl+c -> ctrl+c, super -> super
        run_cmd(["xdotool", "key", keys])
        return {"status": "ok"}

    def window_list(self, params: dict) -> dict:
        r = run_cmd(["wmctrl", "-l"])
        if r.returncode != 0:
            # Fallback: xdotool
            r = run_cmd(["xdotool", "search", "--onlyvisible", "--name", ""])
            if r.returncode != 0:
                return {"error": "Cannot list windows (need wmctrl or xdotool)"}
            windows = [{"id": wid} for wid in r.stdout.strip().split("\n") if wid]
            return {"windows": windows}
        windows = []
        for line in r.stdout.strip().split("\n"):
            parts = line.split(None, 3)
            if len(parts) >= 4:
                windows.append({"id": parts[0], "desktop": parts[1], "host": parts[2], "title": parts[3]})
        return {"windows": windows}

    def window_focus(self, params: dict) -> dict:
        wid = params.get("window_id", "")
        if wid.startswith("0x"):
            run_cmd(["xdotool", "windowactivate", wid])
        else:
            run_cmd(["xdotool", "search", "--name", wid, "windowactivate"])
        return {"status": "ok"}

    def window_resize(self, params: dict) -> dict:
        wid = params.get("window_id", "")
        w, h = params.get("x2", 800), params.get("y2", 600)
        if wid.startswith("0x"):
            run_cmd(["xdotool", "windowsize", wid, str(w), str(h)])
        else:
            run_cmd(["xdotool", "search", "--name", wid, "windowsize", str(w), str(h)])
        return {"status": "ok"}

    def window_close(self, params: dict) -> dict:
        wid = params.get("window_id", "")
        if wid.startswith("0x"):
            run_cmd(["xdotool", "windowclose", wid])
        else:
            run_cmd(["xdotool", "search", "--name", wid, "windowclose"])
        return {"status": "ok"}

    def clipboard_get(self, params: dict) -> dict:
        r = run_cmd(["xclip", "-selection", "clipboard", "-o"])
        if r.returncode != 0:
            r = run_cmd(["xsel", "--clipboard", "--output"])
        return {"content": r.stdout, "status": "ok"}

    def clipboard_set(self, params: dict) -> dict:
        text = params.get("text", "")
        subprocess.run(["xclip", "-selection", "clipboard"], input=text, text=True)
        return {"status": "ok"}

    def screen_info(self, params: dict) -> dict:
        r = run_cmd(["xdotool", "getdisplaygeometry"])
        if r.returncode == 0:
            w, h = r.stdout.strip().split()
            return {"width": int(w), "height": int(h), "display": "x11"}
        return {"error": "Cannot get screen info"}


# ─── Linux Wayland Backend ────────────────────────────────────────────────────

class WaylandBackend:
    """Wayland control via grim, slurp, wl-clipboard, ydotool/wtype, hyprctl."""

    def __init__(self):
        self.is_hyprland = PLATFORM.get("compositor") == "hyprland"

    def screenshot(self, params: dict) -> dict:
        save_path = params.get("save_path") or str(
            default_output_dir() / f"screenshot_{int(time.time())}.png"
        )
        if params.get("fullscreen", True) and not all(
            params.get(k) is not None for k in ("x", "y", "x2", "y2")
        ):
            if which("grim"):
                run_cmd(["grim", save_path])
            else:
                return {"error": "grim not found"}
        else:
            x, y = params["x"], params["y"]
            w = params["x2"] - x if params.get("x2") else 100
            h = params["y2"] - y if params.get("y2") else 100
            region = f"{x},{y} {w}x{h}"
            if which("grim"):
                run_cmd(["grim", "-g", region, save_path])
            else:
                return {"error": "grim not found"}
        return {"path": save_path, "status": "ok"}

    def mouse_click(self, params: dict) -> dict:
        x, y = params.get("x"), params.get("y")
        button = params.get("button", "left")
        clicks = max(1, int(params.get("clicks", 1)))
        btn = YDO_BTN.get(button, 0x00)

        if not which("ydotool"):
            return {"error": "No Wayland input tool found (need ydotool)"}
        if x is not None and y is not None:
            ok, err = run_checked(["ydotool", "mousemove", "--absolute", "-x", str(x), "-y", str(y)])
            if not ok:
                return {"error": f"mousemove failed: {err} (is ydotoold running?)"}
        # -D 30 = 30ms between down/up so QML/GTK widgets register a real tap
        ok, err = run_checked(["ydotool", "click", "-D", "30", "-r", str(clicks), hex(0xC0 | btn)])
        if not ok:
            return {"error": f"click failed: {err} (is ydotoold running?)"}
        return {"status": "ok"}

    def mouse_move(self, params: dict) -> dict:
        if not which("ydotool"):
            return {"error": "ydotool required"}
        ok, err = run_checked(["ydotool", "mousemove", "--absolute", "-x", str(params["x"]), "-y", str(params["y"])])
        return {"status": "ok"} if ok else {"error": f"mousemove failed: {err}"}

    def mouse_drag(self, params: dict) -> dict:
        if not which("ydotool"):
            return {"error": "ydotool required"}
        x, y, x2, y2 = params["x"], params["y"], params["x2"], params["y2"]
        steps = 12  # interpolated motion so drag targets actually track
        cmds = [
            ["ydotool", "mousemove", "--absolute", "-x", str(x), "-y", str(y)],
            ["ydotool", "click", hex(0x40 | YDO_BTN["left"])],
        ]
        for i in range(1, steps + 1):
            ix = x + (x2 - x) * i // steps
            iy = y + (y2 - y) * i // steps
            cmds.append(["ydotool", "mousemove", "--absolute", "-x", str(ix), "-y", str(iy)])
        cmds.append(["ydotool", "click", hex(0x80 | YDO_BTN["left"])])
        for cmd in cmds:
            ok, err = run_checked(cmd)
            if not ok:
                return {"error": f"drag failed at {' '.join(cmd[:2])}: {err}"}
        return {"status": "ok"}

    def mouse_scroll(self, params: dict) -> dict:
        if not which("ydotool"):
            return {"error": "ydotool required"}
        direction = params.get("direction", "down")
        amount = params.get("amount", 3)
        # ponytail: ydotool 1.0.4 has no wheel command — emulate with arrow keys
        code = {"up": 103, "down": 108, "left": 105, "right": 106}.get(direction)
        if code is None:
            return {"error": f"Unknown scroll direction: {direction}"}
        seq: list[str] = []
        for _ in range(max(1, int(amount)) * 3):  # one wheel click ≈ 3 arrow presses
            seq += [f"{code}:1", f"{code}:0"]
        ok, err = run_checked(["ydotool", "key", "-d", "15", *seq])
        return {"status": "ok"} if ok else {"error": f"scroll failed: {err}"}

    def keyboard_type(self, params: dict) -> dict:
        text = params.get("text", "")
        if which("ydotool"):
            ok, err = run_checked(["ydotool", "type", text])
            if not ok and which("wtype"):
                ok, err = run_checked(["wtype", "--", text])
        elif which("wtype"):
            ok, err = run_checked(["wtype", "--", text])
        else:
            return {"error": "Need wtype or ydotool for Wayland typing"}
        return {"status": "ok"} if ok else {"error": f"type failed: {err}"}

    def keyboard_key(self, params: dict) -> dict:
        keys = params.get("keys", "")
        if not which("ydotool"):
            return {"error": "ydotool required for Wayland key events"}
        parts = [p.strip().lower() for p in keys.split("+") if p.strip()]
        if not parts:
            return {"error": "Empty keys parameter"}
        seq: list[str] = []
        held: list[int] = []
        for p in parts:
            code = KEYCODES.get(p)
            if code is None:
                return {"error": f"Unknown key '{p}' (Wayland backend uses raw keycodes)"}
            if p in MOD_KEYS and p != parts[-1]:
                seq.append(f"{code}:1")
                held.append(code)
            else:
                seq += [f"{code}:1", f"{code}:0"]
        seq += [f"{c}:0" for c in reversed(held)]
        ok, err = run_checked(["ydotool", "key", "-d", "20", *seq])
        return {"status": "ok"} if ok else {"error": f"key failed: {err}"}

    def window_list(self, params: dict) -> dict:
        if self.is_hyprland:
            r = run_cmd(["hyprctl", "clients", "-j"])
            if r.returncode == 0:
                clients = json.loads(r.stdout)
                windows = [
                    {
                        "id": str(c.get("address", "")),
                        "title": c.get("title", ""),
                        "class": c.get("class", ""),
                        "workspace": c.get("workspace", {}).get("name", ""),
                    }
                    for c in clients
                ]
                return {"windows": windows}
        return {"error": "Window listing requires Hyprland or compatible compositor"}

    def window_focus(self, params: dict) -> dict:
        wid = params.get("window_id", "")
        if self.is_hyprland:
            if wid.startswith("0x"):
                run_cmd(["hyprctl", "dispatch", "focuswindow", f"address:{wid}"])
            else:
                run_cmd(["hyprctl", "dispatch", "focuswindow", wid])
            return {"status": "ok"}
        return {"error": "Hyprland required"}

    def window_resize(self, params: dict) -> dict:
        wid = params.get("window_id", "")
        w, h = params.get("x2", 800), params.get("y2", 600)
        if self.is_hyprland:
            run_cmd(["hyprctl", "dispatch", "resizewindow", f"{w} {h}"])
            return {"status": "ok"}
        return {"error": "Hyprland required"}

    def window_close(self, params: dict) -> dict:
        wid = params.get("window_id", "")
        if self.is_hyprland:
            if wid.startswith("0x"):
                run_cmd(["hyprctl", "dispatch", "closewindow", f"address:{wid}"])
            else:
                run_cmd(["hyprctl", "dispatch", "closewindow", wid])
            return {"status": "ok"}
        return {"error": "Hyprland required"}

    def clipboard_get(self, params: dict) -> dict:
        r = run_cmd(["wl-paste"])
        return {"content": r.stdout, "status": "ok"}

    def clipboard_set(self, params: dict) -> dict:
        text = params.get("text", "")
        subprocess.run(["wl-copy"], input=text, text=True)
        return {"status": "ok"}

    def screen_info(self, params: dict) -> dict:
        if self.is_hyprland:
            r = run_cmd(["hyprctl", "monitors", "-j"])
            if r.returncode == 0:
                monitors = json.loads(r.stdout)
                info = []
                for m in monitors:
                    info.append({
                        "name": m.get("name"),
                        "width": m.get("width"),
                        "height": m.get("height"),
                        "refresh": m.get("refreshRate"),
                        "focused": m.get("focused", False),
                    })
                return {"monitors": info, "display": "wayland/hyprland"}
        # Fallback: wlr-randr
        if which("wlr-randr"):
            r = run_cmd(["wlr-randr"])
            return {"raw": r.stdout, "display": "wayland"}
        return {"error": "Cannot get screen info"}


# ─── Windows Backend ──────────────────────────────────────────────────────────

class WindowsBackend:
    """Windows control via PowerShell + .NET interop."""

    def _ps(self, script: str) -> subprocess.CompletedProcess:
        return run_powershell(script)

    def screenshot(self, params: dict) -> dict:
        save_path = params.get("save_path") or str(
            default_output_dir() / f"screenshot_{int(time.time())}.png"
        )
        # Convert to Windows path if needed
        save_path = save_path.replace("/", "\\")

        if params.get("fullscreen", True):
            script = f"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$screen = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bitmap = New-Object System.Drawing.Bitmap($screen.Width, $screen.Height)
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.CopyFromScreen($screen.Location, [System.Drawing.Point]::Empty, $screen.Size)
$bitmap.Save('{save_path}', [System.Drawing.Imaging.ImageFormat]::Png)
$graphics.Dispose()
$bitmap.Dispose()
Write-Output "ok"
"""
        else:
            x, y = params["x"], params["y"]
            w = params.get("x2", 100) - x if params.get("x2") else 100
            h = params.get("y2", 100) - y if params.get("y2") else 100
            script = f"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$bitmap = New-Object System.Drawing.Bitmap({w}, {h})
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.CopyFromScreen({x}, {y}, 0, 0, (New-Object System.Drawing.Size({w},{h})))
$bitmap.Save('{save_path}', [System.Drawing.Imaging.ImageFormat]::Png)
$graphics.Dispose()
$bitmap.Dispose()
Write-Output "ok"
"""
        r = self._ps(script)
        if r.returncode == 0:
            return {"path": save_path, "status": "ok"}
        return {"error": r.stderr}

    def mouse_click(self, params: dict) -> dict:
        x, y = params.get("x"), params.get("y")
        button = params.get("button", "left")
        clicks = params.get("clicks", 1)
        btn_map = {"left": "Left", "right": "Right", "middle": "Middle"}
        btn = btn_map.get(button, "Left")

        move = ""
        if x is not None and y is not None:
            move = f"[System.Windows.Forms.Cursor]::Position = New-Object System.Drawing.Point({x},{y})"

        script = f"""
Add-Type -AssemblyName System.Windows.Forms
{move}
Add-Type @'
using System;
using System.Runtime.InteropServices;
public class MouseSim {{
    [DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, int dwExtraInfo);
    public static void Click(string button, int clicks) {{
        uint down, up;
        if (button == "Right") {{ down = 0x0008; up = 0x0010; }}
        else if (button == "Middle") {{ down = 0x0020; up = 0x0040; }}
        else {{ down = 0x0002; up = 0x0004; }}
        for (int i = 0; i < clicks; i++) {{
            mouse_event(down, 0, 0, 0, 0);
            mouse_event(up, 0, 0, 0, 0);
        }}
    }}
}}
'@
[MouseSim]::Click("{btn}", {clicks})
Write-Output "ok"
"""
        r = self._ps(script)
        return {"status": "ok"} if r.returncode == 0 else {"error": r.stderr}

    def mouse_move(self, params: dict) -> dict:
        script = f"""
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.Cursor]::Position = New-Object System.Drawing.Point({params['x']},{params['y']})
Write-Output "ok"
"""
        self._ps(script)
        return {"status": "ok"}

    def mouse_drag(self, params: dict) -> dict:
        x, y, x2, y2 = params["x"], params["y"], params["x2"], params["y2"]
        script = f"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type @'
using System;
using System.Runtime.InteropServices;
public class DragSim {{
    [DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, int dwExtraInfo);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
    public static void Drag(int x1, int y1, int x2, int y2) {{
        SetCursorPos(x1, y1);
        mouse_event(0x0002, 0, 0, 0, 0); // down
        SetCursorPos(x2, y2);
        mouse_event(0x0004, 0, 0, 0, 0); // up
    }}
}}
'@
[DragSim]::Drag({x}, {y}, {x2}, {y2})
Write-Output "ok"
"""
        self._ps(script)
        return {"status": "ok"}

    def mouse_scroll(self, params: dict) -> dict:
        direction = params.get("direction", "down")
        amount = params.get("amount", 3)
        delta = amount * 120 if direction == "up" else -(amount * 120)
        script = f"""
Add-Type @'
using System;
using System.Runtime.InteropServices;
public class ScrollSim {{
    [DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, int dwExtraInfo);
    public static void Scroll(int delta) {{ mouse_event(0x0800, 0, 0, (uint)delta, 0); }}
}}
'@
[ScrollSim]::Scroll({delta})
Write-Output "ok"
"""
        self._ps(script)
        return {"status": "ok"}

    def keyboard_type(self, params: dict) -> dict:
        text = params.get("text", "").replace("'", "''")
        script = f"""
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.SendKeys]::SendWait('{text}')
Write-Output "ok"
"""
        self._ps(script)
        return {"status": "ok"}

    def keyboard_key(self, params: dict) -> dict:
        keys = params.get("keys", "")
        # Convert common combos to SendKeys format
        key_map = {
            "ctrl": "^", "alt": "%", "shift": "+",
            "enter": "{ENTER}", "return": "{ENTER}", "tab": "{TAB}",
            "escape": "{ESC}", "esc": "{ESC}", "backspace": "{BS}",
            "delete": "{DELETE}", "del": "{DELETE}",
            "home": "{HOME}", "end": "{END}",
            "pageup": "{PGUP}", "pagedown": "{PGDN}",
            "up": "{UP}", "down": "{DOWN}", "left": "{LEFT}", "right": "{RIGHT}",
            "space": " ",
        }
        parts = keys.lower().split("+")
        sendkeys = ""
        for p in parts[:-1]:
            sendkeys += key_map.get(p, p)
        last = parts[-1] if parts else ""
        if last.startswith("f") and last[1:].isdigit():
            sendkeys += "{" + last.upper() + "}"
        else:
            sendkeys += key_map.get(last, last)

        script = f"""
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.SendKeys]::SendWait('{sendkeys}')
Write-Output "ok"
"""
        self._ps(script)
        return {"status": "ok"}

    def window_list(self, params: dict) -> dict:
        script = """
Get-Process | Where-Object { $_.MainWindowTitle -ne '' } | Select-Object Id, MainWindowTitle | ConvertTo-Json
"""
        r = self._ps(script)
        if r.returncode == 0 and r.stdout.strip():
            try:
                data = json.loads(r.stdout)
                if isinstance(data, dict):
                    data = [data]
                windows = [{"id": str(w.get("Id", "")), "title": w.get("MainWindowTitle", "")} for w in data]
                return {"windows": windows}
            except json.JSONDecodeError:
                pass
        return {"windows": []}

    def window_focus(self, params: dict) -> dict:
        wid = params.get("window_id", "")
        script = f"""
Add-Type @'
using System;
using System.Runtime.InteropServices;
public class WinFocus {{
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
}}
'@
[WinFocus]::SetForegroundWindow([IntPtr]{wid})
Write-Output "ok"
"""
        self._ps(script)
        return {"status": "ok"}

    def window_resize(self, params: dict) -> dict:
        wid = params.get("window_id", "")
        w, h = params.get("x2", 800), params.get("y2", 600)
        script = f"""
Add-Type @'
using System;
using System.Runtime.InteropServices;
public class WinResize {{
    [DllImport("user32.dll")] public static extern bool MoveWindow(IntPtr hWnd, int X, int Y, int nWidth, int nHeight, bool bRepaint);
}}
'@
[WinResize]::MoveWindow([IntPtr]{wid}, 0, 0, {w}, {h}, true)
Write-Output "ok"
"""
        self._ps(script)
        return {"status": "ok"}

    def window_close(self, params: dict) -> dict:
        wid = params.get("window_id", "")
        script = f"""
Stop-Process -Id {wid} -Force
Write-Output "ok"
"""
        self._ps(script)
        return {"status": "ok"}

    def clipboard_get(self, params: dict) -> dict:
        script = "Get-Clipboard"
        r = self._ps(script)
        return {"content": r.stdout, "status": "ok"}

    def clipboard_set(self, params: dict) -> dict:
        text = params.get("text", "").replace("'", "''")
        script = f"Set-Clipboard -Value '{text}'"
        self._ps(script)
        return {"status": "ok"}

    def screen_info(self, params: dict) -> dict:
        script = """
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.Screen]::AllScreens | ForEach-Object {
    @{ Name = $_.DeviceName; Width = $_.Bounds.Width; Height = $_.Bounds.Height; Primary = $_.Primary }
} | ConvertTo-Json
"""
        r = self._ps(script)
        if r.returncode == 0:
            try:
                data = json.loads(r.stdout)
                if isinstance(data, dict):
                    data = [data]
                return {"monitors": data, "display": "win32"}
            except json.JSONDecodeError:
                pass
        return {"error": "Cannot get screen info"}


# ─── Screen Parse (Element Detection) ─────────────────────────────────────────

class X11ScreenParser:
    """X11 element detection via AT-SPI2 accessibility tree + xdotool geometry."""

    def screen_parse(self, params: dict) -> dict:
        filter_type = params.get("filter_type", "").lower()
        max_elements = params.get("max_elements", 50)
        elements: list[dict] = []

        # Method 1: AT-SPI2 via dbus (works for GTK/Qt apps that expose a11y)
        if which("python3"):
            atspi_script = '''
import json, sys
try:
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    def walk(node, depth=0):
        if not node or depth > 8:
            return []
        results = []
        role = node.get_role_name() or ""
        name = node.get_name() or ""
        try:
            comp = node.get_component_iface()
            if comp:
                bbox = comp.get_extents(Atspi.CoordType.SCREEN)
                x, y, w, h = bbox.x, bbox.y, bbox.width, bbox.height
                if w > 0 and h > 0 and name:
                    results.append({
                        "type": role,
                        "label": name,
                        "bbox": [x, y, x + w, y + h],
                        "center": [x + w // 2, y + h // 2]
                    })
        except Exception:
            pass
        count = node.get_child_count()
        for i in range(min(count, 200)):
            child = node.get_child_at_index(i)
            if child:
                results.extend(walk(child, depth + 1))
        return results

    desktop = Atspi.get_desktop(0)
    elems = walk(desktop)
    print(json.dumps(elems[:200]))
except Exception as e:
    print(json.dumps({"error": str(e)}))
'''
            r = run_cmd(["python3", "-c", atspi_script], timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                try:
                    parsed = json.loads(r.stdout.strip())
                    if isinstance(parsed, list):
                        elements.extend(parsed)
                except json.JSONDecodeError:
                    pass

        # Method 2: Fallback — xdotool window geometry as coarse elements
        if not elements:
            r = run_cmd(["xdotool", "search", "--onlyvisible", "--name", "", "getwindowgeometry", "--shell"])
            if r.returncode == 0:
                current: dict = {}
                for line in r.stdout.strip().split("\n"):
                    if "=" in line:
                        k, v = line.split("=", 1)
                        current[k] = v
                    if "WINDOW" in current and "X" in current and "Y" in current and "WIDTH" in current and "HEIGHT" in current:
                        x, y = int(current["X"]), int(current["Y"])
                        w, h = int(current["WIDTH"]), int(current["HEIGHT"])
                        elements.append({
                            "type": "Window",
                            "label": current.get("WINDOW", ""),
                            "bbox": [x, y, x + w, y + h],
                            "center": [x + w // 2, y + h // 2]
                        })
                        current = {}

        # Method 3: OCR with tesseract for text positions
        if which("tesseract") and which("scrot"):
            tmp_img = tempfile.mktemp(suffix=".png")
            run_cmd(["scrot", tmp_img])
            r = run_cmd(["tesseract", tmp_img, "stdout", "--psm", "11", "tsv"], timeout=30)
            if r.returncode == 0:
                lines = r.stdout.strip().split("\n")
                if len(lines) > 1:
                    headers = lines[0].split("\t")
                    for row in lines[1:max_elements + 1]:
                        cols = row.split("\t")
                        if len(cols) >= len(headers):
                            data = dict(zip(headers, cols))
                            text = data.get("text", "").strip()
                            if text and float(data.get("conf", "0")) > 30:
                                x, y = int(data["left"]), int(data["top"])
                                w, h = int(data["width"]), int(data["height"])
                                elements.append({
                                    "type": "Text",
                                    "label": text,
                                    "bbox": [x, y, x + w, y + h],
                                    "center": [x + w // 2, y + h // 2]
                                })
            Path(tmp_img).unlink(missing_ok=True)

        # Apply filters
        if filter_type:
            elements = [e for e in elements if e.get("type", "").lower() == filter_type]

        return {"elements": elements[:max_elements], "count": len(elements), "method": "atspi+ocr"}


class WaylandScreenParser:
    """Hyprland/Generic Wayland element detection via hyprctl + tesseract OCR."""

    def __init__(self):
        self.is_hyprland = PLATFORM.get("compositor") == "hyprland"

    def screen_parse(self, params: dict) -> dict:
        filter_type = params.get("filter_type", "").lower()
        max_elements = params.get("max_elements", 50)
        elements: list[dict] = []

        # Method 1: Hyprland window geometry
        if self.is_hyprland:
            r = run_cmd(["hyprctl", "clients", "-j"])
            if r.returncode == 0:
                try:
                    clients = json.loads(r.stdout)
                    for c in clients:
                        x, y = c.get("at", [0, 0])
                        w, h = c.get("size", [0, 0])
                        title = c.get("title", "") or c.get("class", "")
                        if w > 0 and h > 0:
                            elements.append({
                                "type": "Window",
                                "label": title,
                                "class": c.get("class", ""),
                                "workspace": c.get("workspace", {}).get("name", ""),
                                "bbox": [x, y, x + w, y + h],
                                "center": [x + w // 2, y + h // 2]
                            })
                except json.JSONDecodeError:
                    pass

        # Method 2: OCR with tesseract for text positions on screen
        if which("tesseract") and which("grim"):
            tmp_img = tempfile.mktemp(suffix=".png")
            run_cmd(["grim", tmp_img])
            r = run_cmd(["tesseract", tmp_img, "stdout", "--psm", "11", "tsv"], timeout=30)
            if r.returncode == 0:
                lines = r.stdout.strip().split("\n")
                if len(lines) > 1:
                    headers = lines[0].split("\t")
                    for row in lines[1:max_elements + 1]:
                        cols = row.split("\t")
                        if len(cols) >= len(headers):
                            data = dict(zip(headers, cols))
                            text = data.get("text", "").strip()
                            if text and float(data.get("conf", "0")) > 30:
                                x, y = int(data["left"]), int(data["top"])
                                w, h = int(data["width"]), int(data["height"])
                                elements.append({
                                    "type": "Text",
                                    "label": text,
                                    "bbox": [x, y, x + w, y + h],
                                    "center": [x + w // 2, y + h // 2]
                                })
            Path(tmp_img).unlink(missing_ok=True)

        if filter_type:
            elements = [e for e in elements if e.get("type", "").lower() == filter_type]

        method = "hyprctl+ocr" if self.is_hyprland else "ocr"
        return {"elements": elements[:max_elements], "count": len(elements), "method": method}


class WindowsScreenParser:
    """Windows element detection via UI Automation (UIA) — no ML deps needed."""

    def screen_parse(self, params: dict) -> dict:
        filter_type = params.get("filter_type", "")
        max_elements = params.get("max_elements", 50)

        # ponytail: use PowerShell + UIAutomation COM directly, no pip install needed
        filter_clause = f'Where-Object {{ $_.ControlType -eq "{filter_type}" }} |' if filter_type else ""

        script = f"""
$ErrorActionPreference = 'SilentlyContinue'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$ae = [System.Windows.Automation.AutomationElement]
$root = $ae::RootElement

$condition = [System.Windows.Automation.Condition]::TrueCondition
$all = $root.FindAll(
    [System.Windows.Automation.TreeScope]::Children,
    $condition
)

$results = @()
foreach ($win in $all) {{
    try {{
        $rect = $win.Current.BoundingRectangle
        if ($rect.IsEmpty -or $rect.Width -le 0 -or $rect.Height -le 0) {{ continue }}
        $results += [PSCustomObject]@{{
            type   = $win.Current.ControlType.ProgrammaticName -replace '^ControlType[.]', ''
            label  = $win.Current.Name
            bbox   = @([int]$rect.X, [int]$rect.Y, [int]($rect.X + $rect.Width), [int]($rect.Y + $rect.Height))
            center = @([int]($rect.X + $rect.Width / 2), [int]($rect.Y + $rect.Height / 2))
        }}

        # Walk children (depth-limited to avoid infinite recursion)
        $children = $win.FindAll(
            [System.Windows.Automation.TreeScope]::Descendants,
            $condition
        )
        $count = 0
        foreach ($child in $children) {{
            if ($count -ge {max_elements}) {{ break }}
            try {{
                $cr = $child.Current.BoundingRectangle
                if ($cr.IsEmpty -or $cr.Width -le 0 -or $cr.Height -le 0) {{ continue }}
                $cname = $child.Current.Name
                $ctype = $child.Current.ControlType.ProgrammaticName -replace '^ControlType[.]', ''
                if ($cname -or $ctype -in @('Button','Edit','MenuItem','CheckBox','RadioButton','TabItem')) {{
                    {(f'if ($ctype -ne "{filter_type}") {{ continue }}' if filter_type else '')}
                    $results += [PSCustomObject]@{{
                        type   = $ctype
                        label  = $cname
                        bbox   = @([int]$cr.X, [int]$cr.Y, [int]($cr.X + $cr.Width), [int]($cr.Y + $cr.Height))
                        center = @([int]($cr.X + $cr.Width / 2), [int]($cr.Y + $cr.Height / 2))
                    }}
                    $count++
                }}
            }} catch {{}}
        }}
    }} catch {{}}
}}
$results | Select-Object -First {max_elements} | ConvertTo-Json -Depth 3
"""
        r = run_powershell(script, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            try:
                data = json.loads(r.stdout)
                if isinstance(data, dict):
                    data = [data]
                return {
                    "elements": data[:max_elements],
                    "count": len(data),
                    "method": "uia"
                }
            except json.JSONDecodeError:
                pass

        # Fallback: just window positions via WinForms
        script_fb = """
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.Screen]::AllScreens | ForEach-Object {{
    Get-Process | Where-Object {{ $_.MainWindowTitle -ne '' }} | ForEach-Object {{
        $proc = $_
        try {{
            Add-Type -AssemblyName UIAutomationClient
            $ae = [System.Windows.Automation.AutomationElement]::FromHandle($proc.MainWindowHandle)
            $rect = $ae.Current.BoundingRectangle
            [PSCustomObject]@{{
                type = 'Window'; label = $proc.MainWindowTitle
                bbox = @([int]$rect.X,[int]$rect.Y,[int]($rect.X+$rect.Width),[int]($rect.Y+$rect.Height))
                center = @([int]($rect.X+$rect.Width/2),[int]($rect.Y+$rect.Height/2))
            }}
        }} catch {{}}
    }}
}} | ConvertTo-Json -Depth 3
"""
        r = run_powershell(script_fb, timeout=20)
        if r.returncode == 0 and r.stdout.strip():
            try:
                data = json.loads(r.stdout)
                if isinstance(data, dict):
                    data = [data]
                return {"elements": data[:max_elements], "count": len(data), "method": "uia_fallback"}
            except json.JSONDecodeError:
                pass

        return {"elements": [], "count": 0, "method": "uia", "error": "UIA parsing failed"}


# ─── Dispatcher ───────────────────────────────────────────────────────────────

class CompositeBackend:
    """Wraps a control backend + screen parser into one dispatch target."""

    def __init__(self, control: Any, parser: Any):
        self._control = control
        self._parser = parser

    def __getattr__(self, name: str) -> Any:
        # screen_parse goes to parser, everything else to control backend
        if name == "screen_parse":
            return getattr(self._parser, name)
        return getattr(self._control, name)


def get_backend() -> CompositeBackend:
    if PLATFORM["os"] == "windows":
        return CompositeBackend(WindowsBackend(), WindowsScreenParser())
    elif PLATFORM["display"] == "wayland":
        return CompositeBackend(WaylandBackend(), WaylandScreenParser())
    else:
        return CompositeBackend(X11Backend(), X11ScreenParser())


# ─── Web Page Screenshot (Playwright) ────────────────────────────────────────

# Prefer system Chrome/Chromium over Playwright's bundled build so we never
# depend on `playwright install` having been run.
_CHROMIUM_CHANNELS = ("chrome", "chromium", "msedge")
_CHROMIUM_BINS = (
    "google-chrome-stable", "google-chrome", "chromium", "chromium-browser",
    "brave-browser", "msedge",
)

DEFAULT_CDP_PORT = 9222

# Headless Chrome defaults WebGL to SwiftShader (CPU rasterizer) — any animated
# canvas then pegs a core. Force ANGLE onto the real GL driver so headless uses
# the same GPU path as a normal window. Keep in sync with
# engine/browser/session.py.
# NOTE: --use-angle=swiftshader is the OPPOSITE of this — it causes the CPU burn.
_GPU_ARGS = [
    "--use-angle=gl",
    "--enable-webgl",
    "--ignore-gpu-blocklist",
    "--enable-gpu-rasterization",
    "--enable-zero-copy",
    "--disable-software-rasterizer",
]


def _gpu_args(headless: bool) -> list[str]:
    """GPU-forcing flags for headless launches; headed already has a real GPU."""
    return list(_GPU_ARGS) if headless else []


def _launch_chromium(p, headless: bool):
    args = _gpu_args(headless)
    last_err: Exception | None = None
    for channel in _CHROMIUM_CHANNELS:
        try:
            return p.chromium.launch(headless=headless, channel=channel, args=args)
        except Exception as e:  # noqa: BLE001 - probe each channel
            last_err = e
    try:
        return p.chromium.launch(headless=headless, args=args)
    except Exception as e:  # noqa: BLE001
        last_err = e
    raise RuntimeError(
        f"no usable Chromium (tried {', '.join(_CHROMIUM_CHANNELS)} + bundled): {last_err}"
    )


def _num(params: dict, key: str, default: float) -> float:
    """Read a numeric param without letting 0 fall through to the default."""
    val = params.get(key)
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


# ─── Persistent Browser (CDP) ────────────────────────────────────────────────

def _browser_dir() -> Path:
    d = Path.home() / "sable_output" / "browser"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _state_path() -> Path:
    return _browser_dir() / "state.json"


def _read_state() -> dict:
    try:
        return json.loads(_state_path().read_text())
    except Exception:  # noqa: BLE001
        return {}


def _write_state(state: dict) -> None:
    try:
        _state_path().write_text(json.dumps(state, indent=2))
    except Exception:  # noqa: BLE001
        pass


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def _find_chrome_bin() -> str | None:
    for b in _CHROMIUM_BINS:
        path = shutil.which(b)
        if path:
            return path
    # Windows fallbacks
    if os.name == "nt":
        for c in (
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        ):
            if Path(c).exists():
                return c
    return None


def _spawn_browser(port: int, headless: bool, user_data_dir: str | None = None) -> dict:
    """Start ONE detached Chrome with a remote-debugging port. Reuses if port busy."""
    if _port_open(port):
        st = _read_state()
        if st.get("port") == port:
            return {"reused": True, "pid": st.get("pid"), "port": port, "user_data_dir": st.get("user_data_dir")}
        return {"reused": True, "pid": None, "port": port, "user_data_dir": None,
                "note": "port already served by a browser Sable did not start"}

    binary = _find_chrome_bin()
    if not binary:
        raise RuntimeError("no Chrome/Chromium binary found on PATH")

    udd = str(Path(user_data_dir).expanduser()) if user_data_dir else str(_browser_dir() / "profile")
    Path(udd).mkdir(parents=True, exist_ok=True)

    cmd = [
        binary,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={udd}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--hide-scrollbars",
    ]
    if headless:
        cmd.append("--headless=new")
        cmd += _gpu_args(headless)
    if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
        cmd.append("--no-sandbox")
    cmd.append("about:blank")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=(os.name != "nt"),
    )

    deadline = time.time() + 15
    while time.time() < deadline:
        if _port_open(port):
            break
        if proc.poll() is not None:
            raise RuntimeError(f"browser exited immediately (code {proc.returncode})")
        time.sleep(0.2)
    else:
        raise RuntimeError(f"browser did not open CDP port {port} within 15s")

    state = {"pid": proc.pid, "port": port, "user_data_dir": udd, "headless": headless,
             "started_at": int(time.time())}
    _write_state(state)
    return {"reused": False, **state}


def _stop_browser(port: int | None = None) -> dict:
    """Kill the tracked browser (SIGTERM then SIGKILL). One instance only."""
    st = _read_state()
    pid = st.get("pid")
    port = port or st.get("port") or DEFAULT_CDP_PORT
    killed = False
    if pid and _pid_alive(pid):
        try:
            os.killpg(os.getpgid(pid), 15) if os.name != "nt" else os.kill(pid, 15)
        except Exception:  # noqa: BLE001
            try:
                os.kill(pid, 15)
            except Exception:  # noqa: BLE001
                pass
        for _ in range(25):
            if not _pid_alive(pid):
                break
            time.sleep(0.2)
        if _pid_alive(pid):
            try:
                os.killpg(os.getpgid(pid), 9) if os.name != "nt" else os.kill(pid, 9)
            except Exception:  # noqa: BLE001
                pass
        killed = True
    _write_state({})
    return {"status": "closed" if killed else "nothing-to-close", "port": port, "pid": pid}


# ─── web_screenshot / web_browser ────────────────────────────────────────────

def web_screenshot(params: dict) -> dict:
    """Render a URL in Chromium and save a PNG.

    Params:
      url                 required; https:// is added if scheme is missing
      delay               seconds to sleep AFTER load before shooting (default 1.5)
      wait_until          load | domcontentloaded | networkidle | commit (default load)
      wait_for_selector   CSS selector to wait for before shooting
      width / height      viewport size (default 1440x900)
      full_page           capture the whole scrollable page (default true)
      headless            default true
      timeout_ms          navigation timeout in ms (default 30000)
      device_scale_factor default 1
      user_agent          override UA string
      dark_mode           emulate prefers-color-scheme: dark
      keep_alive          reuse/keep ONE browser running for later runs (default false)
      cdp_port            remote-debugging port for keep_alive (default 9222)
      user_data_dir       profile dir for the persistent browser
      fresh_page          open a brand-new page instead of reusing the last one
      save_path           output PNG path
    """
    url = str(params.get("url") or "").strip()
    if not url:
        return {"error": "web_screenshot requires 'url'"}
    if "://" not in url:
        url = "https://" + url

    save_path = params.get("save_path") or str(
        default_output_dir() / f"web_{int(time.time())}.png"
    )
    Path(save_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    save_path = str(Path(save_path).expanduser())

    width = int(_num(params, "width", 1440))
    height = int(_num(params, "height", 900))
    delay = _num(params, "delay", 1.5)
    timeout_ms = int(_num(params, "timeout_ms", 30000))
    scale = _num(params, "device_scale_factor", 1)

    wait_until = str(params.get("wait_until") or "load").lower()
    if wait_until not in ("load", "domcontentloaded", "networkidle", "commit"):
        wait_until = "load"

    full_page = bool(params.get("full_page", True))
    headless = bool(params.get("headless", True))
    dark = bool(params.get("dark_mode", False))
    keep_alive = bool(params.get("keep_alive", False))
    fresh_page = bool(params.get("fresh_page", False))
    port = int(_num(params, "cdp_port", DEFAULT_CDP_PORT))
    selector = params.get("wait_for_selector") or None
    user_agent = params.get("user_agent") or None
    udd = params.get("user_data_dir") or None

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"error": "playwright not installed in this Python env"}

    warnings: list[str] = []
    started = time.time()

    with sync_playwright() as p:
        # ── persistent (CDP) mode ──
        if keep_alive:
            try:
                info = _spawn_browser(port, headless, udd)
            except RuntimeError as e:
                return {"error": str(e)}
            try:
                browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=15000)
            except Exception as e:  # noqa: BLE001
                return {"error": f"CDP connect failed on port {port}: {e}"}

            try:
                ctx = browser.contexts[0] if browser.contexts else browser.new_context(
                    viewport={"width": width, "height": height},
                    device_scale_factor=max(scale, 0.5),
                    color_scheme="dark" if dark else "light",
                    user_agent=user_agent,
                )
                pages = ctx.pages
                if fresh_page or not pages:
                    page = ctx.new_page()
                else:
                    page = pages[-1]
                page.set_default_timeout(timeout_ms)

                # A reused Chrome context keeps the OS window size and ignores
                # the context viewport, so pin the page explicitly.
                try:
                    page.set_viewport_size({"width": width, "height": height})
                except Exception as e:  # noqa: BLE001
                    warnings.append(f"viewport: {str(e).splitlines()[0][:160]}")

                resp = None
                try:
                    resp = page.goto(url, wait_until=wait_until, timeout=timeout_ms)
                except Exception as e:  # noqa: BLE001
                    warnings.append(f"navigation: {str(e).splitlines()[0][:200]}")
                if selector:
                    try:
                        page.wait_for_selector(selector, timeout=timeout_ms)
                    except Exception as e:  # noqa: BLE001
                        warnings.append(f"selector {selector!r}: {str(e).splitlines()[0][:160]}")
                if delay > 0:
                    page.wait_for_timeout(int(delay * 1000))

                page.screenshot(path=save_path, full_page=full_page)
                try:
                    title = page.title()
                except Exception:  # noqa: BLE001
                    title = ""

                result: dict[str, Any] = {
                    "path": save_path,
                    "status": "ok",
                    "url": page.url,
                    "title": title,
                    "http_status": resp.status if resp is not None else None,
                    "viewport": f"{width}x{height}",
                    "full_page": full_page,
                    "delay": delay,
                    "elapsed": round(time.time() - started, 2),
                    "browser": {
                        "mode": "persistent",
                        "kept_alive": True,
                        "reused": info.get("reused", False),
                        "pid": info.get("pid"),
                        "port": port,
                        "user_data_dir": info.get("user_data_dir"),
                        "pages_open": len(ctx.pages),
                    },
                }
                if warnings:
                    result["warnings"] = warnings
                # Deliberately NO browser.close() — the whole point is to keep it.
                return result
            finally:
                try:
                    browser.close()
                except Exception:  # noqa: BLE001 - disconnect only, chrome survives
                    pass

        # ── ephemeral mode (one-shot, browser dies with the run) ──
        try:
            browser = _launch_chromium(p, headless)
        except RuntimeError as e:
            return {"error": str(e)}
        try:
            context = browser.new_context(
                viewport={"width": width, "height": height},
                device_scale_factor=max(scale, 0.5),
                color_scheme="dark" if dark else "light",
                user_agent=user_agent,
            )
            page = context.new_page()
            page.set_default_timeout(timeout_ms)

            resp = None
            try:
                resp = page.goto(url, wait_until=wait_until, timeout=timeout_ms)
            except Exception as e:  # noqa: BLE001 - shoot whatever rendered anyway
                warnings.append(f"navigation: {str(e).splitlines()[0][:200]}")

            if selector:
                try:
                    page.wait_for_selector(selector, timeout=timeout_ms)
                except Exception as e:  # noqa: BLE001
                    warnings.append(f"selector {selector!r}: {str(e).splitlines()[0][:160]}")

            if delay > 0:
                page.wait_for_timeout(int(delay * 1000))

            page.screenshot(path=save_path, full_page=full_page)

            try:
                title = page.title()
            except Exception:  # noqa: BLE001
                title = ""
            final_url = page.url

            result = {
                "path": save_path,
                "status": "ok",
                "url": final_url,
                "title": title,
                "http_status": resp.status if resp is not None else None,
                "viewport": f"{width}x{height}",
                "full_page": full_page,
                "delay": delay,
                "elapsed": round(time.time() - started, 2),
                "browser": {"mode": "ephemeral", "kept_alive": False},
            }
            if warnings:
                result["warnings"] = warnings
            context.close()
            return result
        finally:
            browser.close()


def web_browser(params: dict) -> dict:
    """Lifecycle for the ONE persistent browser.

    Params:
      op          status | start | close   (default status)
      cdp_port    default 9222
      headless    default true
      user_data_dir
      pages       for status: list open page URLs (default true)
    """
    op = str(params.get("op") or "status").lower()
    port = int(_num(params, "cdp_port", DEFAULT_CDP_PORT))
    st = _read_state()

    if op == "close":
        return _stop_browser(port)

    if op == "start":
        try:
            info = _spawn_browser(
                port,
                bool(params.get("headless", True)),
                params.get("user_data_dir") or None,
            )
        except RuntimeError as e:
            return {"error": str(e)}
        return {"status": "running", **info}

    if op != "status":
        return {"error": f"unknown op {op!r} (use status|start|close)"}

    running = _port_open(port)
    out: dict[str, Any] = {
        "status": "running" if running else "stopped",
        "port": port,
        "pid": st.get("pid"),
        "pid_alive": _pid_alive(st.get("pid") or 0),
        "user_data_dir": st.get("user_data_dir"),
    }
    if running and params.get("pages", True):
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                b = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=8000)
                urls: list[str] = []
                for ctx in b.contexts:
                    urls.extend(pg.url for pg in ctx.pages)
                out["pages"] = urls
                try:
                    b.close()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001
            out["pages_error"] = str(e).splitlines()[0][:160]
    return out


# ─── Entrypoint ──────────────────────────────────────────────────────────────

# Actions that do not belong to a platform-specific desktop backend.
_BROWSER_ACTIONS = {"web_screenshot": web_screenshot, "web_browser": web_browser}


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "No JSON input provided"}))
        sys.exit(1)

    try:
        params = json.loads(sys.argv[1])
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"Invalid JSON: {e}"}))
        sys.exit(1)

    action = params.get("action")
    if not action:
        print(json.dumps({"error": "Missing 'action' parameter"}))
        sys.exit(1)

    handler = _BROWSER_ACTIONS.get(action)
    if handler is None:
        backend = get_backend()
        handler = getattr(backend, action, None)
        if handler is None:
            print(json.dumps({"error": f"Unknown action: {action}", "platform": PLATFORM}))
            sys.exit(1)

    try:
        result = handler(params)
        result["platform"] = PLATFORM
        print(json.dumps(result, indent=2))
        if result.get("error"):
            sys.exit(2)
    except Exception as e:
        print(json.dumps({"error": str(e), "action": action, "platform": PLATFORM}))
        sys.exit(1)


if __name__ == "__main__":
    main()
