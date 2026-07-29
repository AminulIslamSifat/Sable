"""File operations endpoints."""

import shutil
from pathlib import Path
from fastapi import APIRouter, HTTPException

from engine.skills import BACKUP_DIR
from server.models import RevertRequest

router = APIRouter()


@router.post("/api/file/revert")
def revert_file(payload: RevertRequest) -> dict[str, str]:
    backup = Path(payload.backup_path).expanduser()
    target = Path(payload.path).expanduser()
    # Only allow restoring from the managed backup directory
    try:
        backup.resolve().relative_to(BACKUP_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Backup outside managed directory")
    if not backup.is_file():
        raise HTTPException(status_code=404, detail="Backup not found")
    try:
        shutil.copy2(backup, target)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Revert failed: {exc}")
    return {"status": "ok"}