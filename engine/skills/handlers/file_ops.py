
"""Native editor tag handlers: view_file, edit_file, create_file, insert_file."""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Generator
from pathlib import Path
from typing import Any

from engine.skills.handlers.common import (
    is_ssd_tree_write,
    RESULT_PREVIEW_CHARS,
    _end_event,
    _output_event,
    build_file_edit_event,
    make_backup,
    run_editor,
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

    args = ["view", path]
    start_line = attrs.get("start")
    end_line = attrs.get("end")
    full = attrs.get("full", "").lower() in ("true", "1", "yes")
    if start_line:
        args += ["--start", str(start_line)]
    if end_line:
        args += ["--end", str(end_line)]
    if full:
        args.append("--full")

    ok, output = run_editor(args)
    output_trimmed = output[:RESULT_PREVIEW_CHARS]
    yield _output_event(tag_id, output_trimmed + "\n")
    yield _end_event(tag_id, name, ok, started, {"path": path}, None if ok else output_trimmed[:500])


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

    if is_ssd_tree_write(path):
        yield _output_event(tag_id, "[BLOCKED] Cannot edit files in the protected tree directly.\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Blocked: protected tree write guard")
        return

    if not content.strip():
        yield _output_event(tag_id, "No SEARCH/REPLACE blocks in edit_file body\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Empty edit body")
        return

    replace_all = attrs.get("replace_all", "").lower() in ("true", "1", "yes")
    dry_run = attrs.get("dry_run", "").lower() in ("true", "1", "yes")

    backup_path = make_backup(path) if not dry_run else None
    args = ["edit", path]
    if replace_all:
        args.append("--replace-all")
    if dry_run:
        args.append("--dry-run")
    ok, output = run_editor(args, stdin_data=content)
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

    if is_ssd_tree_write(path):
        yield _output_event(tag_id, "[BLOCKED] Cannot edit files in the protected tree directly.\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Blocked: protected tree write guard")
        return
    overwrite = attrs.get("overwrite", "").lower() in ("true", "1", "yes")

    args = ["create", path]
    if overwrite:
        args.append("--overwrite")

    backup_path = make_backup(path) if overwrite else None
    ok, output = run_editor(args, stdin_data=content)
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

    if is_ssd_tree_write(path):
        yield _output_event(tag_id, "[BLOCKED] Cannot edit files in the protected tree directly.\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Blocked: protected tree write guard")
        return

    at_line = attrs.get("at_line") or attrs.get("at-line")
    after_str = attrs.get("after_str") or attrs.get("after-str")
    dry_run = attrs.get("dry_run", "").lower() in ("true", "1", "yes")

    if not at_line and not after_str:
        yield _output_event(tag_id, "insert_file requires at_line or after_str attribute\n", "stderr")
        yield _end_event(tag_id, name, False, started, error="Missing at_line or after_str")
        return

    args = ["insert", path]
    if dry_run:
        args.append("--dry-run")
    backup_path = make_backup(path) if not dry_run else None
    tmp_anchor: Path | None = None
    tmp_content: Path | None = None
    try:
        tmp_content = Path("/tmp") / f"sable_insert_{uuid.uuid4().hex}.txt"
        tmp_content.write_text(content, encoding="utf-8")
        args += ["--content-file", str(tmp_content)]

        if at_line:
            args += ["--at-line", str(at_line)]
        elif after_str:
            tmp_anchor = Path("/tmp") / f"sable_anchor_{uuid.uuid4().hex}.txt"
            tmp_anchor.write_text(after_str, encoding="utf-8")
            args += ["--after-file", str(tmp_anchor)]

        ok, output = run_editor(args)
    finally:
        for tmp in (tmp_anchor, tmp_content):
            if tmp and tmp.exists():
                try:
                    tmp.unlink()
                except Exception:
                    pass

    output_trimmed = output[:RESULT_PREVIEW_CHARS]
    yield _output_event(tag_id, output_trimmed + "\n")

    if ok:
        file_event = build_file_edit_event(tag_id, "insert", path, output_trimmed, backup_path)
        if file_event is not None:
            yield file_event

    yield _end_event(tag_id, name, ok, started, {"path": path}, None if ok else output_trimmed[:500])
