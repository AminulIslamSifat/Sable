
"""File I/O handlers: get_file, create_note, save_svg."""

from __future__ import annotations

import base64
import json
import mimetypes
import shutil
import subprocess
import time
import uuid
from collections.abc import Generator
from pathlib import Path
from typing import Any

from engine.skills.handlers.common import (
    ASSETS_DIR,
    DEFAULT_TIMEOUT,
    EDITOR_TOOLS,
    MAX_TEXT_BYTES,
    NOTES_DIR,
    PREVIEW_BYTES,
    RESULT_PREVIEW_CHARS,
    TEXT_EXTENSIONS,
    UPLOAD_DIR,
    _end_event,
    _output_event,
    safe_under,
)


def handle_get_file(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()
    raw = content.strip() or attrs.get("path", "")
    if not raw:
        yield _output_event(tag_id, "No path provided\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Empty path")
        return

    path = Path(raw).expanduser()
    if not path.exists():
        yield _output_event(tag_id, f"Path not found: {path}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Path not found")
        return

    if path.is_dir():
        cmd = ["python3", str(EDITOR_TOOLS), "view", str(path)]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=DEFAULT_TIMEOUT,
                errors="replace",
            )
            output = proc.stdout or proc.stderr
            yield _output_event(tag_id, output[:RESULT_PREVIEW_CHARS] + "\n")
            yield _end_event(
                tag_id,
                name,
                proc.returncode == 0,
                started,
                {"path": str(path), "kind": "directory"},
                None if proc.returncode == 0 else f"exit code {proc.returncode}",
            )
        except Exception as exc:
            yield _output_event(tag_id, f"{type(exc).__name__}: {exc}\n", "stderr")
            yield _end_event(tag_id, name, False, started, error=str(exc))
        return

    size = path.stat().st_size
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    ext = path.suffix.lower()
    if mime.startswith("image/"):
        kind = "image"
    elif mime.startswith("text/") or ext in TEXT_EXTENSIONS or path.name.lower() in {"makefile", "dockerfile"}:
        kind = "text"
    else:
        kind = "binary"

    result: dict[str, Any] = {"path": str(path), "size": size, "mime": mime, "kind": kind}

    if kind == "image":
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        dest = UPLOAD_DIR / f"{uuid.uuid4().hex}{ext or '.img'}"
        shutil.copy2(path, dest)
        result["url"] = f"/system/uploads/{dest.name}"
        yield _output_event(tag_id, f"image {path} ({size} bytes) copied to {result['url']}\n")
    elif kind == "text":
        try:
            if size <= MAX_TEXT_BYTES:
                text = path.read_text(errors="replace")
            else:
                with path.open("r", errors="replace") as handle:
                    text = handle.read(PREVIEW_BYTES)
                result["truncated"] = True
            result["preview_chars"] = len(text)
            preview = text
            if len(preview) > RESULT_PREVIEW_CHARS:
                preview = preview[:RESULT_PREVIEW_CHARS] + f"\n... truncated ({len(text)} chars)"
            yield _output_event(tag_id, preview + "\n")
        except Exception as exc:
            yield _output_event(tag_id, f"{type(exc).__name__}: {exc}\n", "stderr")
            yield _end_event(tag_id, name, False, started, error=str(exc))
            return
    else:
        with path.open("rb") as handle:
            head = handle.read(256)
        result["preview_b64"] = base64.b64encode(head).decode()
        yield _output_event(tag_id, f"binary file {path} ({size} bytes, {mime})\n")

    yield _end_event(tag_id, name, True, started, result)


def handle_create_note(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    raw = content.strip()
    note_name = ""
    note_content = ""

    try:
        payload = json.loads(raw)
        if isinstance(payload, dict) and ("content" in payload or "path" in payload or "title" in payload):
            note_name = str(payload.get("path") or payload.get("title") or "")
            note_content = str(payload.get("content", ""))
    except Exception:
        pass

    if not note_name:
        lines = raw.splitlines()
        if len(lines) > 1 and (lines[0].strip().endswith(".md") or "/" in lines[0]):
            note_name = lines[0].strip()
            note_content = "\n".join(lines[1:])
        else:
            note_name = f"note-{time.strftime('%Y%m%d-%H%M%S')}.md"
            note_content = raw

    if not note_name.endswith(".md"):
        note_name += ".md"

    try:
        path = safe_under(NOTES_DIR, note_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(note_content, encoding="utf-8")
    except Exception as exc:
        yield _output_event(tag_id, f"{type(exc).__name__}: {exc}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error=str(exc))
        return

    yield _output_event(tag_id, f"Created note {path} ({len(note_content)} chars)\n")
    yield _end_event(tag_id, name, True, started, {"path": str(path), "chars": len(note_content)})


def handle_save_svg(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    raw = content.strip()
    svg_name = attrs.get("path", "")
    svg = ""

    try:
        payload = json.loads(raw)
        if isinstance(payload, dict) and "svg" in payload:
            svg_name = svg_name or str(payload.get("path", ""))
            svg = str(payload["svg"])
    except Exception:
        pass

    if not svg:
        svg = raw
    if not svg_name:
        svg_name = f"svg-{time.strftime('%Y%m%d-%H%M%S')}.svg"
    if not svg_name.endswith(".svg"):
        svg_name += ".svg"

    if not svg.lstrip().startswith("<svg"):
        yield _output_event(tag_id, "Content does not look like SVG\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Invalid SVG")
        return

    try:
        path = safe_under(ASSETS_DIR, svg_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(svg, encoding="utf-8")
    except Exception as exc:
        yield _output_event(tag_id, f"{type(exc).__name__}: {exc}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error=str(exc))
        return

    yield _output_event(tag_id, f"Saved SVG {path} ({len(svg)} chars)\n")
    yield _end_event(tag_id, name, True, started, {"path": str(path), "chars": len(svg)})
