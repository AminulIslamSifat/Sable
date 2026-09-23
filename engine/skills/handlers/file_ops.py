
"""Native editor tag handlers: view_file, edit_file, create_file, insert_file."""

# Checkpoint handlers are also defined here since they relate to file state management.


from __future__ import annotations

import os
import time
from collections.abc import Generator
from typing import Any

from engine.skills.handlers.common import (
    RESULT_PREVIEW_CHARS,
    _end_event,
    _output_event,
    build_file_edit_event,
    make_backup,
)

# ponytail: direct import instead of subprocess — native call, zero overhead
from tools.code_editor.scripts.editor_tools import (
    ToolError,
    create_file as _create_file,
    edit_file as _edit_file,
    insert_file as _insert_file,
    view_file as _view_file,
    _parse_search_replace_blocks,
)


def handle_view_file(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()
    path = attrs.get("path", "").strip() or content.strip()
    if not path:
        yield _output_event(tag_id, "No path attribute provided\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Missing path")
        return

    path = os.path.expandvars(os.path.expanduser(path))

    start_line = int(attrs["start"]) if attrs.get("start") else None
    end_line = int(attrs["end"]) if attrs.get("end") else None
    full = attrs.get("full", "").lower() in ("true", "1", "yes")

    try:
        output = _view_file(path, start=start_line, end=end_line, full=full)
        output_trimmed = output[:RESULT_PREVIEW_CHARS]
        yield _output_event(tag_id, output_trimmed + "\n")
        yield _end_event(tag_id, name, True, started, {"path": path})
    except ToolError as exc:
        err_msg = str(exc)[:RESULT_PREVIEW_CHARS]
        yield _output_event(tag_id, f"Error: {err_msg}\n", "stderr")
        yield _end_event(tag_id, name, False, started, {"path": path}, error=err_msg[:500])
    except Exception as exc:
        err_msg = f"{type(exc).__name__}: {exc}"
        yield _output_event(tag_id, f"{err_msg}\n", "stderr")
        yield _end_event(tag_id, name, False, started, {"path": path}, error=err_msg[:500])


def handle_edit_file(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()
    path = attrs.get("path", "").strip()
    if not path:
        yield _output_event(tag_id, "No path attribute provided\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Missing path")
        return

    path = os.path.expandvars(os.path.expanduser(path))

    if not content.strip():
        yield _output_event(tag_id, "No SEARCH/REPLACE blocks in edit_file body\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Empty edit body")
        return

    replace_all = attrs.get("replace_all", "").lower() in ("true", "1", "yes")
    dry_run = attrs.get("dry_run", "").lower() in ("true", "1", "yes")

    backup_path = make_backup(path) if not dry_run else None

    try:
        payload = _parse_search_replace_blocks(content)
        output = _edit_file(path, payload, backup=True, replace_all=replace_all, dry_run=dry_run)
        ok = True
    except ToolError as exc:
        output = f"Error: {exc}"
        ok = False
    except Exception as exc:
        output = f"Internal error: {exc}"
        ok = False

    output_trimmed = output[:RESULT_PREVIEW_CHARS]
    yield _output_event(tag_id, output_trimmed + "\n")

    if ok and not dry_run:
        file_event = build_file_edit_event(tag_id, "edit", path, output_trimmed, backup_path)
        if file_event is not None:
            yield file_event

    yield _end_event(tag_id, name, ok, started, {"path": path}, None if ok else output_trimmed[:500])


def handle_create_file(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()
    path = attrs.get("path", "").strip()
    if not path:
        yield _output_event(tag_id, "No path attribute provided\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Missing path")
        return

    path = os.path.expandvars(os.path.expanduser(path))

    overwrite = attrs.get("overwrite", "").lower() in ("true", "1", "yes")
    dry_run = attrs.get("dry_run", "").lower() in ("true", "1", "yes")

    # dry_run: report intent without touching disk or taking a backup
    if dry_run:
        exists = os.path.exists(path)
        action = "overwrite" if exists else "create"
        would_fail = exists and not overwrite
        msg = (
            f"[dry_run] would {action} {path} ({len(content)} chars)"
            + (" — BLOCKED: file exists and overwrite=false" if would_fail else "")
        )
        yield _output_event(tag_id, msg + "\n", "stderr" if would_fail else "stdout")
        yield _end_event(tag_id, name, not would_fail, started, {"path": path, "dry_run": True, "would_fail": would_fail})
        return

    backup_path = make_backup(path) if overwrite else None

    try:
        output = _create_file(path, content, overwrite=overwrite)
        ok = True
    except ToolError as exc:
        output = f"Error: {exc}"
        ok = False
    except Exception as exc:
        output = f"Internal error: {exc}"
        ok = False

    output_trimmed = output[:RESULT_PREVIEW_CHARS]
    yield _output_event(tag_id, output_trimmed + "\n")

    if ok:
        file_event = build_file_edit_event(tag_id, "create", path, output_trimmed, backup_path)
        if file_event is not None:
            yield file_event

    yield _end_event(tag_id, name, ok, started, {"path": path}, None if ok else output_trimmed[:500])


def handle_insert_file(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    started = time.time()
    path = attrs.get("path", "").strip()
    if not path:
        yield _output_event(tag_id, "No path attribute provided\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Missing path")
        return

    path = os.path.expandvars(os.path.expanduser(path))

    at_line_raw = attrs.get("at_line") or attrs.get("at-line")
    after_str = attrs.get("after_str") or attrs.get("after-str")
    dry_run = attrs.get("dry_run", "").lower() in ("true", "1", "yes")

    if not at_line_raw and not after_str:
        yield _output_event(tag_id, "insert_file requires at_line or after_str attribute\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Missing at_line or after_str")
        return

    # Reject the ambiguous both-given case instead of picking silently
    if at_line_raw and after_str:
        yield _output_event(tag_id, "Provide either at_line or after_str, not both\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="at_line and after_str are mutually exclusive")
        return

    try:
        at_line = int(at_line_raw) if at_line_raw else None
    except (TypeError, ValueError):
        yield _output_event(tag_id, f"Invalid at_line: {at_line_raw!r}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Invalid at_line")
        return

    backup_path = make_backup(path) if not dry_run else None

    try:
        output = _insert_file(path, content, at_line=at_line, after_str=after_str, dry_run=dry_run)
        ok = True
    except ToolError as exc:
        output = f"Error: {exc}"
        ok = False
    except Exception as exc:
        output = f"Internal error: {exc}"
        ok = False

    output_trimmed = output[:RESULT_PREVIEW_CHARS]
    yield _output_event(tag_id, output_trimmed + "\n")

    if ok and not dry_run:
        file_event = build_file_edit_event(tag_id, "insert", path, output_trimmed, backup_path)
        if file_event is not None:
            yield file_event

    yield _end_event(tag_id, name, ok, started, {"path": path, "dry_run": dry_run}, None if ok else output_trimmed[:500])


def handle_list_checkpoints(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    """List checkpoints with SHA, timestamp, message preview, and tool name."""
    started = time.time()
    chat_id = attrs.get("chat_id", "").strip() or None
    limit_str = attrs.get("limit", "20").strip()
    try:
        limit = int(limit_str)
    except ValueError:
        limit = 20

    try:
        from server.database import list_checkpoints_with_preview
        rows = list_checkpoints_with_preview(chat_id=chat_id, limit=limit)
    except Exception as exc:
        yield _output_event(tag_id, f"Error listing checkpoints: {exc}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error=str(exc))
        return

    if not rows:
        yield _output_event(tag_id, "No checkpoints found.\n")
        yield _end_event(tag_id, name, True, started)
        return

    lines = [f"Found {len(rows)} checkpoint(s):\n"]
    for r in rows:
        sha_short = r["sha"][:12] if r.get("sha") else "?"
        ts = r.get("timestamp", "")
        tool = r.get("tool_name", "")
        preview = (r.get("message_preview") or "").replace("\n", " ")[:100]
        lines.append(f"  {sha_short} | {ts} | {tool} | {preview}")
    lines.append("")

    yield _output_event(tag_id, "\n".join(lines) + "\n")
    yield _end_event(tag_id, name, True, started, {"count": len(rows)})


def handle_restore_checkpoint(
    tag_id: str, name: str, attrs: dict[str, str], content: str
) -> Generator[dict[str, Any], None, None]:
    """Restore workspace to a previous checkpoint state."""
    started = time.time()
    sha = attrs.get("sha", "").strip()
    if not sha:
        yield _output_event(tag_id, "No sha attribute provided\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Missing sha")
        return

    # Look up checkpoint metadata to get project_root
    try:
        from server.database import get_checkpoint_by_sha
        cp = get_checkpoint_by_sha(sha)
    except Exception as exc:
        yield _output_event(tag_id, f"Error looking up checkpoint: {exc}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error=str(exc))
        return

    if not cp:
        yield _output_event(tag_id, f"Checkpoint '{sha}' not found in database.\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Checkpoint not found")
        return

    project_root = cp.get("project_root", "")
    if not project_root:
        yield _output_event(tag_id, "Checkpoint has no project_root recorded.\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="No project_root")
        return

    # Perform the restore
    try:
        from engine.checkpoint import get_checkpoint_manager
        mgr = get_checkpoint_manager(project_root)
        result = mgr.restore(sha)
    except Exception as exc:
        yield _output_event(tag_id, f"Restore failed: {exc}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error=str(exc))
        return

    if not result.get("ok"):
        err = result.get("error", "Unknown error")
        yield _output_event(tag_id, f"Restore failed: {err}\n", "stderr")
        yield _end_event(tag_id, name, False, started, error=err)
        return

    # Format diff summary
    diff = result.get("diff", [])
    lines = [f"Restored to checkpoint {sha[:12]}\n"]
    if diff:
        lines.append(f"Files changed: {len(diff)}\n")
        for f in diff[:30]:
            status = f.get("status", "?")
            path = f.get("path", "?")
            adds = f.get("additions", 0)
            dels = f.get("deletions", 0)
            lines.append(f"  [{status}] {path} (+{adds}/-{dels})")
        if len(diff) > 30:
            lines.append(f"  ... and {len(diff) - 30} more files")
    else:
        lines.append("No file differences detected (workspace may already match).")
    lines.append("")

    yield _output_event(tag_id, "\n".join(lines) + "\n")
    yield _end_event(tag_id, name, True, started, {"sha": sha, "files_changed": len(diff)})
