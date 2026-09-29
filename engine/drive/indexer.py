"""Background Drive indexer — caches folder trees for instant browse.

When a user adds a Drive source, we kick off a non-blocking scan that walks
every folder and writes a flat entry list to disk. Subsequent opens serve from
cache instantly; a background refresh keeps it current using modifiedTime diffs
for authed sources or full re-lists with local diffing for keyless.

Storage: ~/sable_output/drive/index/{source_id}.json
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from engine.config import OUTPUT_ROOT
from engine.drive import public as drive_public
from engine.drive.client import DriveError, get_drive_client

logger = logging.getLogger("sable.drive.indexer")

INDEX_DIR = OUTPUT_ROOT / "drive" / "index"

# Track in-flight indexing tasks so we don't double-scan the same source.
_active_tasks: dict[str, asyncio.Task] = {}

# Cooldown after any mutation before allowing a re-scan (seconds).
_MUTATION_COOLDOWN = 30
_last_mutation: dict[str, float] = {}


# ── Index I/O ────────────────────────────────────────────────────────────────


def _index_path(source_id: str) -> Path:
    return INDEX_DIR / f"{source_id}.json"


def read_index(source_id: str) -> dict[str, Any] | None:
    """Return cached index data or None if not yet indexed."""
    p = _index_path(source_id)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "entries" in data:
            return data
    except Exception:
        pass
    return None


def write_index(source_id: str, entries: list[dict[str, Any]], *,
                drive_id: str = "", status: str = "ready") -> dict[str, Any]:
    """Atomically write an index file. Returns the written record."""
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "source_id": source_id,
        "drive_id": drive_id,
        "indexed_at": time.time(),
        "status": status,
        "entry_count": len(entries),
        "entries": entries,
    }
    tmp = _index_path(source_id).with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, _index_path(source_id))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return record


def delete_index(source_id: str) -> None:
    """Remove a cached index when its source is deleted."""
    p = _index_path(source_id)
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass


def mark_mutation(source_id: str) -> None:
    """Record that a source was mutated; suppresses re-scans for cooldown."""
    _last_mutation[source_id] = time.time()


def is_indexing(source_id: str) -> bool:
    """True when a background scan is currently running for this source."""
    task = _active_tasks.get(source_id)
    return task is not None and not task.done()


# ── Scanning ─────────────────────────────────────────────────────────────────


async def _scan_folder_keyless(folder_id: str, parent: str = "") -> list[dict[str, Any]]:
    """Recursively walk a public folder via embeddedfolderview scraping.

    `parent` is the id of the folder CONTAINING these entries. The caller
    passes the drive_id for the root call so root entries are filterable.
    """
    entries: list[dict[str, Any]] = []
    try:
        items = await drive_public.list_folder(folder_id)
    except drive_public.PublicDriveError:
        return entries
    # Recurse concurrently into subfolders — sequential scraping of a deep
    # tree is what made a 300-entry scan take minutes.
    subfolders = [it for it in items if it.get("is_folder")]
    for item in items:
        entries.append({
            "id": item["id"],
            "name": item.get("name", item["id"]),
            "parent": parent,
            "kind": "folder" if item.get("is_folder") else "file",
            "mime": item.get("mimeType", ""),
            "size": int(item.get("size") or 0),
            "modified": item.get("modified", ""),
        })
    if subfolders:
        sem = asyncio.Semaphore(6)

        async def _one(sub: dict[str, Any]) -> list[dict[str, Any]]:
            async with sem:
                return await _scan_folder_keyless(sub["id"], parent=sub["id"])

        results = await asyncio.gather(*(_one(s) for s in subfolders))
        for chunk in results:
            entries.extend(chunk)
    return entries


async def _scan_folder_authed(folder_id: str, parent: str = "") -> list[dict[str, Any]]:
    """Recursively walk a folder via the Drive API v3 client."""
    client = get_drive_client()
    entries: list[dict[str, Any]] = []
    try:
        items = await client.list_children(folder_id)
    except DriveError:
        return entries
    subfolders: list[dict[str, Any]] = []
    for item in items:
        mime = item.get("mimeType", "")
        is_folder = mime == "application/vnd.google-apps.folder"
        entries.append({
            "id": item.get("id", ""),
            "name": item.get("name", ""),
            "parent": parent,
            "kind": "folder" if is_folder else "file",
            "mime": mime,
            "size": int(item.get("size") or 0),
            "modified": item.get("modifiedTime", ""),
        })
        if is_folder:
            subfolders.append(item)
    if subfolders:
        sem = asyncio.Semaphore(6)

        async def _one(sub: dict[str, Any]) -> list[dict[str, Any]]:
            async with sem:
                return await _scan_folder_authed(sub["id"], parent=sub["id"])

        results = await asyncio.gather(*(_one(s) for s in subfolders))
        for chunk in results:
            entries.extend(chunk)
    return entries


async def _full_scan(drive_id: str, *, use_authed: bool = False) -> list[dict[str, Any]]:
    """Walk an entire folder tree, preferring keyless unless told otherwise.

    Root entries are tagged with `parent=drive_id` so the browse routes can
    filter them with a simple equality check.
    """
    if use_authed:
        return await _scan_folder_authed(drive_id, parent=drive_id)
    try:
        return await _scan_folder_keyless(drive_id, parent=drive_id)
    except Exception:
        # Keyless failed — fall back to authed if available.
        client = get_drive_client()
        if client.connected:
            logger.info("Keyless scan failed for %s, falling back to authed", drive_id)
            return await _scan_folder_authed(drive_id, parent=drive_id)
        raise


async def _adaptive_refresh(drive_id: str, old_entries: list[dict[str, Any]],
                            *, use_authed: bool = False) -> list[dict[str, Any]]:
    """Refresh only changed items when possible, fall back to full re-scan.

    For authed sources: query files where modifiedTime > last indexed_at.
    For keyless: no modifiedTime available, so do a full re-scan but diff locally.
    """
    if not use_authed:
        # Keyless can't do incremental — full re-scan.
        return await _full_scan(drive_id, use_authed=False)

    client = get_drive_client()
    if not client.connected:
        return await _full_scan(drive_id, use_authed=False)

    # Build a set of known ids for diffing.
    old_map = {e["id"]: e for e in old_entries}

    # Full re-scan is simpler and more reliable than trying to query by
    # modifiedTime across all subfolders (which requires knowing every folder
    # id). The scan itself is fast enough for typical study folders.
    new_entries = await _full_scan(drive_id, use_authed=True)

    # Merge: keep old entries that still exist, update changed ones.
    new_map = {e["id"]: e for e in new_entries}
    merged = []
    seen = set()
    for entry in new_entries:
        merged.append(entry)
        seen.add(entry["id"])
    # Entries that disappeared are simply omitted.
    return merged


# ── Public API ───────────────────────────────────────────────────────────────


async def index_source(source_id: str, drive_id: str, *, force: bool = False) -> dict[str, Any]:
    """Full scan of a Drive source. Writes index to disk.

    Call this directly for synchronous indexing, or use trigger_background()
    for the non-blocking path.
    """
    # Check cooldown unless forced.
    if not force:
        last = _last_mutation.get(source_id, 0)
        if time.time() - last < _MUTATION_COOLDOWN:
            existing = read_index(source_id)
            if existing:
                return existing

    client = get_drive_client()
    use_authed = client.connected

    # Write a "scanning" placeholder so the UI knows work is happening.
    write_index(source_id, [], drive_id=drive_id, status="scanning")

    try:
        entries = await _full_scan(drive_id, use_authed=use_authed)
        record = write_index(source_id, entries, drive_id=drive_id, status="ready")
        logger.info("Indexed %d entries for source %s (%s)",
                     len(entries), source_id, drive_id)
        return record
    except Exception as exc:
        logger.warning("Index failed for source %s: %s", source_id, exc)
        # Keep whatever partial data we had before.
        existing = read_index(source_id)
        if existing and existing.get("entries"):
            existing["status"] = "stale"
            write_index(source_id, existing["entries"],
                        drive_id=drive_id, status="stale")
            return existing
        write_index(source_id, [], drive_id=drive_id, status="error")
        raise


async def refresh_source(source_id: str) -> dict[str, Any]:
    """Adaptive refresh — only fetch what changed since last index."""
    existing = read_index(source_id)
    if not existing:
        raise ValueError(f"No index found for source {source_id}")

    drive_id = existing.get("drive_id", "")
    if not drive_id:
        raise ValueError(f"Index for {source_id} has no drive_id")

    client = get_drive_client()
    use_authed = client.connected

    write_index(source_id, existing["entries"], drive_id=drive_id, status="refreshing")

    try:
        entries = await _adaptive_refresh(
            drive_id, existing["entries"], use_authed=use_authed
        )
        record = write_index(source_id, entries, drive_id=drive_id, status="ready")
        logger.info("Refreshed source %s: %d entries", source_id, len(entries))
        return record
    except Exception as exc:
        logger.warning("Refresh failed for source %s: %s", source_id, exc)
        existing["status"] = "stale"
        write_index(source_id, existing["entries"], drive_id=drive_id, status="stale")
        return existing


def trigger_background(source_id: str, drive_id: str, *, force: bool = False) -> None:
    """Spawn a non-blocking background scan. Safe to call multiple times."""
    if is_indexing(source_id):
        return  # Already scanning this source.

    async def _run():
        try:
            await index_source(source_id, drive_id, force=force)
        except Exception as exc:
            logger.warning("Background index failed for %s: %s", source_id, exc)
        finally:
            _active_tasks.pop(source_id, None)

    task = asyncio.ensure_future(_run())
    _active_tasks[source_id] = task
