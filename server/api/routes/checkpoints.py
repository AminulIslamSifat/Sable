"""Checkpoint restore API — list, diff preview, and restore endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from server.database import (
    get_checkpoints_for_chat,
    get_checkpoint_by_sha,
    get_latest_checkpoint_for_message,
)
from engine.checkpoint import get_checkpoint_manager

router = APIRouter(prefix="/api/checkpoints", tags=["checkpoints"])


class RestoreRequest(BaseModel):
    commit_sha: str


@router.get("/diff/{commit_sha}")
async def get_diff(commit_sha: str):
    """Get diff preview between a checkpoint and current state."""
    cp = get_checkpoint_by_sha(commit_sha)
    if not cp:
        raise HTTPException(status_code=404, detail="Checkpoint not found")

    mgr = get_checkpoint_manager(cp["project_root"])
    files = mgr.get_diff_stat(commit_sha)
    diff_text = mgr.get_diff_content(commit_sha, max_lines=150)

    return {
        "commit_sha": commit_sha,
        "project_root": cp["project_root"],
        "files": files,
        "diff_text": diff_text,
        "total_files": len(files),
        "total_additions": sum(f["additions"] for f in files),
        "total_deletions": sum(f["deletions"] for f in files),
    }


@router.post("/restore")
async def restore_checkpoint(req: RestoreRequest):
    """Restore project files to a checkpoint state."""
    cp = get_checkpoint_by_sha(req.commit_sha)
    if not cp:
        raise HTTPException(status_code=404, detail="Checkpoint not found")

    mgr = get_checkpoint_manager(cp["project_root"])
    result = mgr.restore(req.commit_sha)

    if not result["ok"]:
        raise HTTPException(status_code=500, detail=result.get("error", "Restore failed"))

    return {
        "ok": True,
        "restored_to": req.commit_sha,
        "project_root": cp["project_root"],
        "diff": result.get("diff", []),
    }


@router.get("/{chat_id}/message/{message_id}")
async def get_checkpoint_for_message(chat_id: str, message_id: int):
    """Get the latest checkpoint at or before a given message."""
    cp = get_latest_checkpoint_for_message(chat_id, message_id)
    if not cp:
        return {"checkpoint": None}
    return {"checkpoint": cp}


@router.get("/{chat_id}")
async def list_checkpoints(chat_id: str):
    """List all checkpoints for a chat."""
    checkpoints = get_checkpoints_for_chat(chat_id)
    return {"checkpoints": checkpoints}
