
"""Filesystem browser — VS Code-style file explorer for Sable UI."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse

router = APIRouter()

from pathlib import Path as _Path

# Allowed browse roots (read-only from UI)
def _detect_wsl2() -> bool:
    """Detect WSL2 at module load time for browse roots."""
    import sys
    if sys.platform == "win32":
        return False
    try:
        return "microsoft" in open("/proc/version", "r").read().lower()
    except Exception:
        return False

import tempfile as _tempfile
_BROWSE_ROOTS = (
    str(_Path.home()),
    _tempfile.gettempdir(),
    *( ("/mnt",) if _detect_wsl2() else () ),
)

# Directories to always hide
_HIDDEN_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", ".sable_backups",
    ".editor_tools_backups",
    ".cache", ".local", ".npm", ".config", ".mozilla", ".thunderbird",
}

# File extensions considered binary / not viewable
_BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
    ".pdf", ".exe", ".dll", ".so", ".pyc", ".pyo",
    ".mp3", ".mp4", ".avi", ".mkv", ".wav", ".flac",
    ".sqlite", ".db",
}

_MAX_FILE_SIZE = 512 * 1024  # 512 KB read limit


def _is_allowed(path: str) -> bool:
    """Check if path is within allowed browse roots."""
    resolved = os.path.normpath(os.path.expanduser(path))
    for root in _BROWSE_ROOTS:
        norm_root = os.path.normpath(root)
        if resolved == norm_root or resolved.startswith(norm_root + os.sep):
            return True
    return False


def _is_hidden(name: str) -> bool:
    """Check if a file/dir should be hidden."""
    return name in _HIDDEN_DIRS


def _file_icon(name: str, is_dir: bool) -> str:
    """Return an emoji icon based on file type."""
    if is_dir:
        return "📁"
    ext = Path(name).suffix.lower()
    icon_map = {
        ".py": "🐍", ".js": "📜", ".ts": "📜", ".tsx": "⚛️", ".jsx": "⚛️",
        ".html": "🌐", ".css": "🎨", ".json": "📋", ".yaml": "📋", ".yml": "📋",
        ".md": "📝", ".txt": "📄", ".toml": "⚙️", ".cfg": "⚙️", ".ini": "⚙️",
        ".sh": "🖥️", ".bash": "🖥️", ".zsh": "🖥️",
        ".sql": "🗃️", ".csv": "📊", ".xml": "📰",
        ".svg": "🖼️", ".png": "🖼️", ".jpg": "🖼️",
        ".lock": "🔒", ".env": "🔑",
    }
    return icon_map.get(ext, "📄")


@router.post("/api/filesystem/write")
async def filesystem_write(request: Request) -> dict[str, Any]:
    """Write content to a file (create or overwrite)."""
    body = await request.json()
    path = body.get("path", "")
    content = body.get("content", "")

    if not path:
        return {"error": "No path provided"}
    if not _is_allowed(path):
        return {"error": "Access denied — path outside allowed roots"}

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.write_text(content, encoding="utf-8")
        return {"ok": True, "path": str(target)}
    except Exception as e:
        return {"error": f"Write failed: {e}"}


@router.post("/api/filesystem/mkdir")
async def filesystem_mkdir(request: Request) -> dict[str, Any]:
    """Create a new directory."""
    body = await request.json()
    path = body.get("path", "")

    if not path:
        return {"error": "No path provided"}
    if not _is_allowed(path):
        return {"error": "Access denied — path outside allowed roots"}

    target = Path(path)
    try:
        target.mkdir(parents=True, exist_ok=True)
        return {"ok": True, "path": str(target)}
    except FileExistsError:
        return {"error": "Directory already exists"}
    except Exception as e:
        return {"error": f"Failed: {e}"}


@router.post("/api/filesystem/copy")
async def filesystem_copy(request: Request) -> dict[str, Any]:
    """Copy a file or directory."""
    import shutil
    body = await request.json()
    src = body.get("path", "")
    dst = body.get("dest", "")

    if not src or not dst:
        return {"error": "Missing path or dest"}
    if not _is_allowed(src) or not _is_allowed(dst):
        return {"error": "Access denied"}

    src_p = Path(src)
    dst_p = Path(dst)
    if not src_p.exists():
        return {"error": "Source not found"}

    try:
        if src_p.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        return {"ok": True, "path": str(dst_p)}
    except Exception as e:
        return {"error": f"Copy failed: {e}"}


@router.post("/api/filesystem/move")
async def filesystem_move(request: Request) -> dict[str, Any]:
    """Move/rename a file or directory."""
    import shutil
    body = await request.json()
    src = body.get("path", "")
    dst = body.get("dest", "")

    if not src or not dst:
        return {"error": "Missing path or dest"}
    if not _is_allowed(src) or not _is_allowed(dst):
        return {"error": "Access denied"}

    src_p = Path(src)
    if not src_p.exists():
        return {"error": "Source not found"}

    try:
        shutil.move(src, dst)
        return {"ok": True, "path": dst}
    except Exception as e:
        return {"error": f"Move failed: {e}"}


@router.post("/api/filesystem/delete")
async def filesystem_delete(request: Request) -> dict[str, Any]:
    """Delete a file or directory."""
    import shutil
    body = await request.json()
    path = body.get("path", "")

    if not path:
        return {"error": "Missing path"}
    if not _is_allowed(path):
        return {"error": "Access denied"}

    target = Path(path)
    if not target.exists():
        return {"error": "Not found"}

    try:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        return {"ok": True}
    except Exception as e:
        return {"error": f"Delete failed: {e}"}


def _get_display_env() -> dict[str, str]:
    """Build env dict with display vars for GUI dialogs.
    
    Server may be started without display env (systemd, autostart).
    Detect the active Hyprland/Wayland session and inject the vars.
    On Windows, returns os.environ as-is (no Wayland/X11 concept).
    """
    import sys
    if sys.platform == "win32":
        return os.environ.copy()
    env = os.environ.copy()
    needed = ["WAYLAND_DISPLAY", "XDG_CURRENT_DESKTOP", "DISPLAY", "XDG_RUNTIME_DIR"]
    missing = [k for k in needed if not env.get(k)]
    if not missing:
        return env
    # Try to find active wayland/x11 session from loginctl
    try:
        sessions = subprocess.run(
            ["loginctl", "list-sessions", "--no-legend"],
            capture_output=True, text=True, timeout=5,
        )
        for line in sessions.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            sid = parts[0]
            sess_info = subprocess.run(
                ["loginctl", "show-session", sid, "-p", "Type", "-p", "Display", "-p", "Remote"],
                capture_output=True, text=True, timeout=5,
            )
            info = dict(l.split("=", 1) for l in sess_info.stdout.strip().splitlines() if "=" in l)
            if info.get("Remote") == "yes":
                continue
            stype = info.get("Type", "")
            if stype in ("wayland", "x11", "tty"):
                # Get env vars from this session's leader process
                leader = subprocess.run(
                    ["loginctl", "show-session", sid, "-p", "Leader"],
                    capture_output=True, text=True, timeout=5,
                )
                leader_pid = leader.stdout.strip().split("=")[-1].strip()
                if leader_pid.isdigit():
                    env_path = f"/proc/{leader_pid}/environ"
                    try:
                        with open(env_path, "rb") as f:
                            for entry in f.read().split(b"\x00"):
                                if b"=" in entry:
                                    k, v = entry.decode(errors="replace").split("=", 1)
                                    if k in missing and k not in env:
                                        env[k] = v
                    except (FileNotFoundError, PermissionError):
                        pass
                # Also check XDG_RUNTIME_DIR from uid
                if "XDG_RUNTIME_DIR" in missing and "XDG_RUNTIME_DIR" not in env:
                    uid_res = subprocess.run(
                        ["loginctl", "show-session", sid, "-p", "User"],
                        capture_output=True, text=True, timeout=5,
                    )
                    uid = uid_res.stdout.strip().split("=")[-1].strip()
                    if uid.isdigit():
                        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
                break
    except Exception:
        pass
    # Fallback guesses
    env.setdefault("XDG_CURRENT_DESKTOP", "Hyprland")
    if not env.get("WAYLAND_DISPLAY"):
        import glob as _glob
        wl_socks = _glob.glob("/run/user/*/wayland-*")
        if wl_socks:
            env["WAYLAND_DISPLAY"] = os.path.basename(wl_socks[0])
    return env


def _is_wsl2() -> bool:
    """Detect if running inside WSL2."""
    try:
        version = open("/proc/version", "r").read().lower()
        return "microsoft" in version and "wsl2" in version or "microsoft-standard" in version
    except Exception:
        return False


def _win_path_to_wsl(win_path: str) -> str:
    r"""Convert Windows path (C:\Users\...) to WSL path (/mnt/c/Users/...)."""
    if not win_path or len(win_path) < 3:
        return win_path
    drive = win_path[0].lower()
    rest = win_path[2:].replace(chr(92), "/")
    return f"/mnt/{drive}{rest}"


@router.get("/api/filesystem/pick-folder")
def filesystem_pick_folder() -> dict[str, Any]:
    """Open native OS folder picker. WSL2 → Windows picker; Linux → xdg-desktop-portal → zenity/yad/kdialog → tkinter."""
    import logging
    log = logging.getLogger("sable.fs")
    display_env = _get_display_env()
    log.info("[pick-folder] Request received, DISPLAY=%s WAYLAND=%s XDG_DESKTOP=%s WSL2=%s",
             display_env.get("DISPLAY", "(unset)"),
             display_env.get("WAYLAND_DISPLAY", "(unset)"),
             display_env.get("XDG_CURRENT_DESKTOP", "(unset)"),
             _is_wsl2())

    # ── 0. WSL2 → Windows native folder picker ──
    if _is_wsl2():
        try:
            log.info("[pick-folder] WSL2 detected, trying Windows native picker")
            ps_cmd = [
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "$shell = New-Object -ComObject Shell.Application; "
                "$folder = $shell.BrowseForFolder(0, 'Open Folder', 0); "
                "if ($folder) { $folder.Self.Path } else { exit 1 }"
            ]
            result = subprocess.run(ps_cmd, capture_output=True, text=True, timeout=120)
            log.info("[pick-folder] Windows picker rc=%d stdout=%r stderr=%r",
                     result.returncode, result.stdout.strip()[:200], result.stderr.strip()[:200])
            if result.returncode == 0 and result.stdout.strip():
                win_path = result.stdout.strip()
                wsl_path = _win_path_to_wsl(win_path)
                if os.path.isdir(wsl_path):
                    log.info("[pick-folder] Windows selected: %s → %s", win_path, wsl_path)
                    return {"path": wsl_path}
                else:
                    log.warning("[pick-folder] Converted path not accessible: %s", wsl_path)
            elif result.returncode == 1:
                log.info("[pick-folder] Windows picker cancelled")
                return {"path": None, "cancelled": True}
        except FileNotFoundError:
            log.warning("[pick-folder] powershell.exe not found in PATH")
        except subprocess.TimeoutExpired:
            log.error("[pick-folder] Windows picker timed out after 120s")
            return {"path": None, "error": "Windows folder picker timed out"}
        except Exception as e:
            log.warning("[pick-folder] Windows picker failed: %s", e)
        # Fall through to Linux methods if Windows picker fails

    # ── 1. xdg-desktop-portal via gdbus (modern Linux, works on Wayland + X11) ──
    try:
        import uuid, json as _json
        token = f"sable_{uuid.uuid4().hex[:8]}"
        sender_name = subprocess.run(
            ["gdbus", "call", "--session",
             "--dest", "org.freedesktop.DBus",
             "--object-path", "/org/freedesktop/DBus",
             "--method", "org.freedesktop.DBus.GetNameOwner",
             "org.freedesktop.portal.Desktop"],
            capture_output=True, text=True, timeout=5, env=display_env,
        )
        if sender_name.returncode == 0:
            log.info("[pick-folder] Trying xdg-desktop-portal")
            options = f"{{'handle_token': <'{token}'>, 'directory': <true>}}"
            call_res = subprocess.run(
                ["gdbus", "call", "--session",
                 "--dest", "org.freedesktop.portal.Desktop",
                 "--object-path", "/org/freedesktop/portal/desktop",
                 "--method", "org.freedesktop.portal.FileChooser.OpenDirectory",
                 "", "Open Folder", options],
                capture_output=True, text=True, timeout=120, env=display_env,
            )
            if call_res.returncode == 0 and "(/org/freedesktop/portal/desktop/request/" in call_res.stdout:
                request_path = call_res.stdout.strip().strip("()'\"").split("'")[1] \
                    if "'" in call_res.stdout else ""
                if request_path:
                    # Wait for the Response signal
                    signal_res = subprocess.run(
                        ["gdbus", "monitor", "--session",
                         "--dest", "org.freedesktop.portal.Desktop",
                         "--object-path", request_path,
                         "--timeout", "120"],
                        capture_output=True, text=True, timeout=125, env=display_env,
                    )
                    for line in signal_res.stdout.splitlines():
                        if "Response" in line and "results" in line:
                            # Extract URI from results
                            import re
                            uris = re.findall(r"'file://([^']+)'", line)
                            if uris:
                                path = uris[0].replace("%20", " ")
                                if os.path.isdir(path):
                                    log.info("[pick-folder] Portal selected: %s", path)
                                    return {"path": path}
                            log.info("[pick-folder] Portal cancelled")
                            return {"path": None, "cancelled": True}
    except Exception as e:
        log.debug("[pick-folder] xdg-desktop-portal failed: %s", e)

    # ── 2. Legacy CLI dialogs (zenity / yad / kdialog) ──
    for cmd in [
        ["zenity", "--file-selection", "--directory", "--title=Open Folder"],
        ["yad", "--file", "--directory", "--title=Open Folder"],
        ["kdialog", "--getexistingdirectory"],
    ]:
        log.info("[pick-folder] Trying: %s", " ".join(cmd))
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=display_env)
            log.info("[pick-folder] rc=%d stdout=%r stderr=%r",
                     result.returncode, result.stdout.strip()[:200], result.stderr.strip()[:200])
            if result.returncode == 0:
                path = result.stdout.strip()
                if path and os.path.isdir(path):
                    log.info("[pick-folder] Selected: %s", path)
                    return {"path": path}
            elif result.returncode == 1:
                log.info("[pick-folder] User cancelled (rc=1)")
                return {"path": None, "cancelled": True}
        except FileNotFoundError:
            log.warning("[pick-folder] %s not found, trying next", cmd[0])
            continue
        except subprocess.TimeoutExpired:
            log.error("[pick-folder] %s timed out after 120s", cmd[0])
            return {"path": None, "error": "Dialog timed out"}

    # ── 3. tkinter fallback (cross-platform, ships with Python) ──
    try:
        import tkinter as tk
        from tkinter import filedialog
        log.info("[pick-folder] Trying tkinter folder chooser")
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(title="Open Folder")
        root.destroy()
        if path:
            log.info("[pick-folder] tkinter selected: %s", path)
            return {"path": path}
        log.info("[pick-folder] tkinter cancelled")
        return {"path": None, "cancelled": True}
    except Exception as e:
        log.debug("[pick-folder] tkinter not available: %s", e)

    log.error("[pick-folder] No dialog method available")
    return {"path": None, "error": "No folder picker available (need xdg-desktop-portal, zenity, or python3-tk)"}


@router.get("/api/filesystem/roots")
def filesystem_roots() -> list[dict[str, Any]]:
    """Return quick-access browse roots."""
    _h = _Path.home()
    quick_paths = [
        str(_h / "Projects" / "Sable"),
        str(_h / "hdd" / "projects" / "Sable"),
        str(_h / "hdd"),
        str(_h / "Projects"),
        str(_h),
        _tempfile.gettempdir(),
    ]
    roots = []
    for r in quick_paths:
        p = Path(r)
        if p.exists():
            roots.append({
                "path": str(p),
                "name": p.name or str(p),
                "label": str(p),
            })
    return roots


@router.get("/api/filesystem/list")
def filesystem_list(path: str = Query(..., description="Directory path to list")) -> dict[str, Any]:
    """List contents of a directory."""
    if not _is_allowed(path):
        return {"error": "Access denied — path outside allowed roots", "items": []}

    target = Path(path)
    if not target.exists():
        return {"error": "Path does not exist", "items": []}
    if not target.is_dir():
        return {"error": "Not a directory", "items": []}

    items: list[dict[str, Any]] = []
    try:
        for entry in sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            if _is_hidden(entry.name):
                continue
            is_dir = entry.is_dir()
            item: dict[str, Any] = {
                "name": entry.name,
                "path": str(entry),
                "is_dir": is_dir,
                "icon": _file_icon(entry.name, is_dir),
            }
            if not is_dir:
                try:
                    item["size"] = entry.stat().st_size
                    item["binary"] = entry.suffix.lower() in _BINARY_EXTS
                except OSError:
                    item["size"] = 0
                    item["binary"] = True
            items.append(item)
    except PermissionError:
        return {"error": "Permission denied", "items": []}

    return {"path": str(target), "items": items}


@router.get("/api/filesystem/read")
def filesystem_read(path: str = Query(..., description="File path to read")) -> dict[str, Any]:
    """Read file content for display."""
    if not _is_allowed(path):
        return {"error": "Access denied — path outside allowed roots"}

    target = Path(path)
    if not target.exists():
        return {"error": "File does not exist"}
    if target.is_dir():
        return {"error": "Cannot read a directory"}

    ext = target.suffix.lower()
    if ext in _BINARY_EXTS:
        return {"error": "Binary file — cannot display", "binary": True}

    try:
        size = target.stat().st_size
        if size > _MAX_FILE_SIZE:
            return {
                "error": f"File too large ({size // 1024} KB) — max 512 KB",
                "truncated": True,
            }
        content = target.read_text(encoding="utf-8", errors="replace")
        return {
            "path": str(target),
            "name": target.name,
            "content": content,
            "size": size,
            "ext": ext,
        }
    except Exception as e:
        return {"error": f"Read failed: {e}"}


# --------------------------------------------------------------------------
# Serve raw file (for HTML preview with working relative paths)
# --------------------------------------------------------------------------

_MIME_MAP = {
    ".html": "text/html", ".htm": "text/html",
    ".css": "text/css", ".js": "application/javascript", ".mjs": "application/javascript",
    ".json": "application/json", ".xml": "application/xml",
    ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
    ".ico": "image/x-icon", ".woff": "font/woff", ".woff2": "font/woff2",
    ".ttf": "font/ttf", ".otf": "font/otf",
    ".txt": "text/plain", ".md": "text/plain",
}


@router.get("/api/filesystem/serve")
def filesystem_serve(path: str = Query(..., description="File path to serve raw")) -> Any:
    """Serve a raw file with correct MIME type. Used for HTML preview."""
    if not _is_allowed(path):
        return {"error": "Access denied — path outside allowed roots"}

    target = Path(path)
    if not target.exists() or not target.is_file():
        return {"error": "File does not exist"}

    ext = target.suffix.lower()
    media_type = _MIME_MAP.get(ext, "application/octet-stream")
    return FileResponse(str(target), media_type=media_type)


@router.get("/api/filesystem/serve-dir/{sub_path:path}")
def filesystem_serve_dir(sub_path: str, base: str = Query(..., description="Base directory")) -> Any:
    """Serve files relative to a base directory. For HTML preview asset resolution."""
    full = os.path.normpath(os.path.join(base, sub_path))
    if not _is_allowed(full):
        return {"error": "Access denied"}

    target = Path(full)
    if not target.exists() or not target.is_file():
        return {"error": "Not found"}

    ext = target.suffix.lower()
    media_type = _MIME_MAP.get(ext, "application/octet-stream")
    return FileResponse(str(target), media_type=media_type)


# --------------------------------------------------------------------------
# Inline diff review (accept/reject edits)
# --------------------------------------------------------------------------

_BACKUP_DIR_NAME = ".editor_tools_backups"
_MAX_DIFF_AGE_SECONDS = 3600  # Only show diffs for edits within last hour


def _find_latest_backup(file_path: str) -> dict[str, Any] | None:
    """Find the most recent backup for a file in its .editor_tools_backups dir."""
    import time as _time

    p = Path(file_path)
    backup_dir = p.parent / _BACKUP_DIR_NAME
    if not backup_dir.is_dir():
        return None

    base = p.name
    candidates = []
    try:
        for f in backup_dir.iterdir():
            if f.name.startswith(base + ".") and f.name.endswith(".bak"):
                candidates.append(f)
    except OSError:
        return None

    if not candidates:
        return None

    # Sort by mtime, newest first
    candidates.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    latest = candidates[0]

    # Only consider recent backups
    age = _time.time() - latest.stat().st_mtime
    if age > _MAX_DIFF_AGE_SECONDS:
        return None

    try:
        content = latest.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    return {
        "backup_path": str(latest),
        "content": content,
        "age_seconds": int(age),
    }


@router.get("/api/filesystem/pending-diff")
def filesystem_pending_diff(path: str = Query(..., description="File path to check")) -> dict[str, Any]:
    """Check if a file has a pending (recent) edit with a backup available."""
    if not _is_allowed(path):
        return {"has_diff": False}

    target = Path(path)
    if not target.exists():
        return {"has_diff": False}

    backup_info = _find_latest_backup(path)
    if not backup_info:
        return {"has_diff": False}

    # Read current file content for comparison
    try:
        current_content = target.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {"has_diff": False}

    # Only show diff if content actually differs
    if current_content == backup_info["content"]:
        return {"has_diff": False}

    return {
        "has_diff": True,
        "backup_path": backup_info["backup_path"],
        "original_content": backup_info["content"],
        "modified_content": current_content,
        "age_seconds": backup_info["age_seconds"],
    }


@router.post("/api/filesystem/accept-edit")
async def filesystem_accept_edit(request: Request) -> dict[str, Any]:
    """Accept an edit — keep the new content, remove the backup."""
    body = await request.json()
    backup_path = body.get("backup_path", "")

    if not backup_path:
        return {"error": "No backup_path provided"}

    bp = Path(backup_path)
    if not bp.exists():
        return {"error": "Backup not found"}

    try:
        bp.unlink()
        return {"ok": True}
    except Exception as e:
        return {"error": f"Failed to remove backup: {e}"}


@router.post("/api/filesystem/reject-edit")
async def filesystem_reject_edit(request: Request) -> dict[str, Any]:
    """Reject an edit — restore backup content to the file, remove the backup."""
    body = await request.json()
    file_path = body.get("path", "")
    backup_path = body.get("backup_path", "")

    if not file_path or not backup_path:
        return {"error": "Missing path or backup_path"}
    if not _is_allowed(file_path):
        return {"error": "Access denied"}

    bp = Path(backup_path)
    target = Path(file_path)

    if not bp.exists():
        return {"error": "Backup not found"}
    if not target.exists():
        return {"error": "Target file not found"}

    try:
        # Restore backup content to the file
        content = bp.read_text(encoding="utf-8", errors="replace")
        target.write_text(content, encoding="utf-8")
        bp.unlink()
        return {"ok": True, "restored": True}
    except Exception as e:
        return {"error": f"Reject failed: {e}"}
