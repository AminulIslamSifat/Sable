
"""Filesystem browser — VS Code-style file explorer for Sable UI."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request

router = APIRouter()

# Allowed browse roots (read-only from UI)
_BROWSE_ROOTS = (
    "/home/sifat",
    "/tmp",
)

# Directories to always hide
_HIDDEN_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", ".sable_backups",
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
    if name.startswith("."):
        return True
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


@router.get("/api/filesystem/pick-folder")
def filesystem_pick_folder() -> dict[str, Any]:
    """Open native folder picker dialog (zenity/yad/kdialog)."""
    for cmd in [
        ["zenity", "--file-selection", "--directory", "--title=Open Folder"],
        ["yad", "--file", "--directory", "--title=Open Folder"],
        ["kdialog", "--getexistingdirectory"],
    ]:
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0:
                path = result.stdout.strip()
                if path and os.path.isdir(path):
                    return {"path": path}
            elif result.returncode == 1:
                # User cancelled
                return {"path": None, "cancelled": True}
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            return {"path": None, "error": "Dialog timed out"}
    return {"path": None, "error": "No file dialog tool found (install zenity)"}


@router.get("/api/filesystem/roots")
def filesystem_roots() -> list[dict[str, Any]]:
    """Return quick-access browse roots."""
    quick_paths = [
        "/home/sifat/Projects/Sable",
        "/home/sifat/hdd/projects/Sable",
        "/home/sifat/hdd",
        "/home/sifat/Projects",
        "/home/sifat",
        "/tmp",
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
