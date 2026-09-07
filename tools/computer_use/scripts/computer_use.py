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


# ─── Dispatcher ───────────────────────────────────────────────────────────────

def get_backend():
    if PLATFORM["os"] == "windows":
        return WindowsBackend()
    elif PLATFORM["display"] == "wayland":
        return WaylandBackend()
    else:
        return X11Backend()


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

    backend = get_backend()
    handler = getattr(backend, action, None)
    if handler is None:
        print(json.dumps({"error": f"Unknown action: {action}", "platform": PLATFORM}))
        sys.exit(1)

    try:
        result = handler(params)
        result["platform"] = PLATFORM
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e), "action": action, "platform": PLATFORM}))
        sys.exit(1)


if __name__ == "__main__":
    main()
