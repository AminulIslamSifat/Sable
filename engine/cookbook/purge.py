"""Cascade purge — remove a model and EVERY trace of it.

A model touches five places. Deleting only the .gguf leaves four ghosts:
  1. system/cookbook_state.json        -> servers[] entries
  2. system/.custom_models.json        -> _cookbook registry entry
  3. system/cookbook_model_settings.json -> per-model instruction card
  4. system/cookbook_logs/<serve-id>.log -> server logs
  5. system/models/llm/<file>.gguf     -> the weights themselves

purge_model() handles all five so the UI can never accumulate tombstones.
"""
from __future__ import annotations
from engine.config import PERSISTENT_ROOT  # frozen-build writable root

import json
import logging
from pathlib import Path

from engine.cookbook.state import get_state
from engine.cookbook.model_settings import delete_model_settings

logger = logging.getLogger("sable.cookbook")


def _label_from_path(path: Path) -> str:
    return path.stem


def _model_id(label: str) -> str:
    return "local/" + label.lower().replace(" ", "-")


def _unregister_custom(model_ids: set[str]) -> int:
    """Remove cookbook-registered entries from .custom_models.json."""
    p = PERSISTENT_ROOT / "system" / ".custom_models.json"
    if not p.exists():
        return 0
    try:
        models = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0
    before = len(models)
    models = [
        m for m in models
        if not (m.get("_cookbook") and (
            m.get("id") in model_ids
            or m.get("id", "").lower() in model_ids
        ))
    ]
    removed = before - len(models)
    if removed:
        p.write_text(json.dumps(models, indent=2), encoding="utf-8")
    return removed


def purge_model(
    *,
    model_path: str = "",
    model_label: str = "",
    delete_file: bool = True,
) -> dict:
    """Remove a model and all associated state.

    Either model_path or model_label identifies the target. Path takes
    precedence for matching; label is used to derive the settings/custom id.
    Returns a summary dict of what was removed.
    """
    state = get_state()
    summary = {
        "servers_stopped": 0,
        "servers_removed": 0,
        "custom_removed": 0,
        "settings_removed": 0,
        "logs_removed": 0,
        "file_deleted": False,
    }

    p = Path(model_path) if model_path else None
    label = model_label or (_label_from_path(p) if p else "")

    # Collect every matching server entry (by path AND by label).
    matches = []
    if p:
        matches += state.find_servers(model_path=str(p))
    if label:
        for s in state.find_servers(model_label=label):
            if s not in matches:
                matches.append(s)

    # 1. Stop any still-running instance.
    from engine.process_utils import kill_process_tree
    for s in matches:
        if s.status == "running" and s.pid > 0:
            try:
                kill_process_tree(s.pid)
                summary["servers_stopped"] += 1
            except Exception:
                pass

    # 2. Remove server entries.
    before = len(state.servers)
    drop_ids = {id(s) for s in matches}
    state.servers = [s for s in state.servers if id(s) not in drop_ids]
    if len(state.servers) != before:
        state.save()
        summary["servers_removed"] = before - len(state.servers)

    # 3. Delete log files for those server ids.
    logs_dir = state.logs_dir
    for s in matches:
        lp = logs_dir / f"{s.id}.log"
        if lp.exists():
            try:
                lp.unlink()
                summary["logs_removed"] += 1
            except OSError:
                pass

    # 4. Unregister from .custom_models.json.
    ids = {_model_id(label)} if label else set()
    for s in matches:
        ids.add(_model_id(s.model_label))
    summary["custom_removed"] = _unregister_custom(ids)

    # 5. Delete per-model settings.
    for mid in ids:
        if delete_model_settings(mid):
            summary["settings_removed"] += 1

    # 6. Delete the weights.
    if delete_file and p and p.exists() and p.suffix == ".gguf":
        try:
            p.unlink()
            summary["file_deleted"] = True
        except OSError as exc:
            logger.warning("Could not delete %s: %s", p, exc)

    logger.info("Purged model '%s': %s", label, summary)
    return summary
