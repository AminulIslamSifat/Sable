"""Saved Drive sources — the cards shown in the Drive tab.

A "source" is a Drive link the user added, remembered so it appears as a card
instead of having to be re-pasted. Only metadata is stored; file bytes are
fetched on demand (viewer streams them, import copies them into the library).

Storage: ~/sable_output/drive/sources.json
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from engine.config import OUTPUT_ROOT

logger = logging.getLogger("sable.drive.store")

DRIVE_DIR = OUTPUT_ROOT / "drive"
SOURCES_PATH = DRIVE_DIR / "sources.json"

_SOURCE_ID_RE = re.compile(r"[0-9a-f]{16}")


def _ensure() -> None:
    DRIVE_DIR.mkdir(parents=True, exist_ok=True)


def _read() -> list[dict[str, Any]]:
    if not SOURCES_PATH.is_file():
        return []
    try:
        data = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _write(items: list[dict[str, Any]]) -> None:
    """Atomic write at 0600 — sources are personal, not secret, but private."""
    _ensure()
    tmp = SOURCES_PATH.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(items, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, SOURCES_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def list_sources() -> list[dict[str, Any]]:
    """All saved sources, newest first."""
    items = _read()
    items.sort(key=lambda s: s.get("added", 0), reverse=True)
    return items


def get_source(source_id: str) -> dict[str, Any] | None:
    if not _SOURCE_ID_RE.fullmatch(source_id or ""):
        return None
    for s in _read():
        if s.get("id") == source_id:
            return s
    return None


def add_source(
    *,
    url: str,
    drive_id: str,
    name: str,
    kind: str,
    count: int = 0,
    account: str = "",
) -> dict[str, Any]:
    """Save a link. Adding the same drive id twice refreshes the existing card."""
    items = _read()
    for s in items:
        if s.get("drive_id") == drive_id:
            s.update({"url": url, "name": name or s.get("name"), "kind": kind,
                      "count": count, "account": account, "updated": time.time()})
            _write(items)
            return s
    src = {
        "id": uuid.uuid4().hex[:16],
        "url": url,
        "drive_id": drive_id,
        "name": name or drive_id,
        "kind": kind,          # 'folder' | 'file'
        "count": count,
        "account": account,
        "added": time.time(),
    }
    items.append(src)
    _write(items)
    return src


def remove_source(source_id: str) -> bool:
    if not _SOURCE_ID_RE.fullmatch(source_id or ""):
        return False
    items = _read()
    kept = [s for s in items if s.get("id") != source_id]
    if len(kept) == len(items):
        return False
    _write(kept)
    return True
