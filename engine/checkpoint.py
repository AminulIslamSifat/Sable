
"""CheckpointManager — shadow-git based project snapshots per chat turn.

Creates a separate git repo (outside the project) and commits the full
workspace state after every file-mutating tool call. Restore = checkout
from shadow repo back into the working directory.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _get_checkpoint_base() -> Path:
    """Platform-appropriate checkpoint storage directory."""
    if platform.system() == "Windows":
        # %APPDATA%/sable/checkpoints
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "sable" / "checkpoints"
        return Path.home() / "AppData" / "Roaming" / "sable" / "checkpoints"
    elif platform.system() == "Darwin":
        # ~/Library/Application Support/sable/checkpoints
        return Path.home() / "Library" / "Application Support" / "sable" / "checkpoints"
    else:
        # Linux/other: XDG standard
        xdg = os.environ.get("XDG_DATA_HOME")
        if xdg:
            return Path(xdg) / "sable" / "checkpoints"
        return Path.home() / ".local" / "share" / "sable" / "checkpoints"


# Where shadow repos live
CHECKPOINT_BASE = _get_checkpoint_base()

# Files/dirs to exclude from snapshots
EXCLUDE_PATTERNS = [
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".sable_backups",
    ".editor_tools_backups",
    "*.pyc",
    ".pytest_cache",
]

# Max file size to track (10MB)
MAX_FILE_SIZE = 10 * 1024 * 1024


class CheckpointManager:
    """Manages shadow git checkpoints for a single project root."""

    def __init__(self, project_root: str):
        self.project_root = Path(project_root).resolve().as_posix()
        self._hash = hashlib.sha256(self.project_root.encode()).hexdigest()[:12]
        self.git_dir = (CHECKPOINT_BASE / self._hash / ".git").as_posix()
        self._ensure_shadow_repo()

    def _git(self, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
        cmd = [
            "git",
            f"--git-dir={self.git_dir}",
            f"--work-tree={self.project_root}",
            *args,
        ]
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )

    def _ensure_shadow_repo(self) -> None:
        if not os.path.exists(self.git_dir):
            os.makedirs(os.path.dirname(self.git_dir), exist_ok=True)
            # Init bare repo — use posix path so git doesn't choke on backslashes
            result = subprocess.run(
                ["git", "init", "--bare", self.git_dir],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                logger.error("Failed to init shadow repo at %s: %s", self.git_dir, result.stderr[:300])
            # Set up excludes
            self._write_excludes()
        # Ensure identity is set (bare repos don't always inherit global config)
        email = self._git("config", "user.email")
        if email.returncode != 0 or not email.stdout.strip():
            self._git("config", "user.email", "sable@checkpoint.local")
        name = self._git("config", "user.name")
        if name.returncode != 0 or not name.stdout.strip():
            self._git("config", "user.name", "Sable Checkpoint")

    def _write_excludes(self) -> None:
        """Write a git exclude file (like .gitignore but internal to shadow repo)."""
        # self.git_dir is already posix; use os.path.join for filesystem ops
        excludes_path = Path(os.path.join(self.git_dir, "info", "exclude"))
        excludes_path.parent.mkdir(parents=True, exist_ok=True)
        excludes_path.write_text("\n".join(EXCLUDE_PATTERNS) + "\n")

    def save_checkpoint(self, chat_id: str, message_id: int, tool_name: str) -> str | None:
        """Commit current workspace state. Returns commit SHA or None on failure."""
        try:
            # Stage all changes (respects excludes)
            result = self._git("add", "-A", timeout=60)
            if result.returncode != 0:
                logger.warning("checkpoint add failed: %s", result.stderr[:200])
                return None

            # Commit (allow empty for initial state capture)
            msg = f"checkpoint:{chat_id}:{message_id}:{tool_name}"
            result = self._git(
                "commit", "--allow-empty", "-m", msg,
                timeout=60,
            )
            if result.returncode != 0 and "nothing to commit" not in result.stdout:
                logger.warning("checkpoint commit failed: %s", result.stderr[:200])
                return None

            # Get the SHA
            result = self._git("rev-parse", "HEAD")
            if result.returncode != 0:
                return None
            sha = result.stdout.strip()
            logger.info("Checkpoint saved: %s (%s)", sha[:8], msg)
            return sha

        except subprocess.TimeoutExpired:
            logger.warning("checkpoint timed out for %s", self.project_root)
            return None
        except Exception as e:
            logger.error("checkpoint error: %s", e)
            return None

    def restore(self, commit_sha: str) -> dict[str, Any]:
        """Restore workspace to a checkpoint. Returns summary dict."""
        try:
            # Get diff stats before restoring
            diff_stat = self.get_diff_stat(commit_sha)

            # Checkout all files from that commit
            result = self._git("checkout", commit_sha, "--", ".")
            if result.returncode != 0:
                return {"ok": False, "error": result.stderr[:500]}

            # Clean files that didn't exist at that point.
            # Exclude system/ — it holds sessions, configs, and credentials
            # that must survive checkpoint restores.
            result = self._git("clean", "-fd", "-e", "system/")
            if result.returncode != 0:
                logger.warning("clean after restore: %s", result.stderr[:200])

            return {"ok": True, "diff": diff_stat}

        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "Restore timed out"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_diff_stat(self, commit_sha: str) -> list[dict[str, Any]]:
        """Get file-level diff between current state and a checkpoint.

        Returns list of {path, status, additions, deletions}.
        """
        try:
            # First stage current state so diff works
            self._git("add", "-A", timeout=60)

            # Diff between checkpoint and current (staged)
            result = self._git(
                "diff", "--numstat", commit_sha, "--cached", timeout=30
            )
            if result.returncode != 0:
                # Fallback: try without --cached
                result = self._git("diff", "--numstat", commit_sha, timeout=30)

            files = []
            for line in result.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                parts = line.split("\t")
                if len(parts) >= 3:
                    adds = int(parts[0]) if parts[0] != "-" else 0
                    dels = int(parts[1]) if parts[1] != "-" else 0
                    path = parts[2]
                    status = "modified"
                    if adds > 0 and dels == 0:
                        status = "added"
                    elif dels > 0 and adds == 0:
                        status = "deleted"
                    files.append({
                        "path": path,
                        "status": status,
                        "additions": adds,
                        "deletions": dels,
                    })
            return files

        except Exception as e:
            logger.error("diff_stat error: %s", e)
            return []

    def get_diff_content(self, commit_sha: str, max_lines: int = 200) -> str:
        """Get human-readable diff between checkpoint and current state."""
        try:
            self._git("add", "-A", timeout=60)
            result = self._git(
                "diff", commit_sha, "--cached", "--stat", timeout=30
            )
            stat_output = result.stdout or ""

            result = self._git(
                "diff", commit_sha, "--cached", timeout=30
            )
            diff_output = result.stdout or ""

            # Truncate if too long
            lines = diff_output.split("\n")
            if len(lines) > max_lines:
                diff_output = "\n".join(lines[:max_lines]) + f"\n... ({len(lines) - max_lines} more lines)"

            return f"=== Summary ===\n{stat_output}\n\n=== Diff ===\n{diff_output}"

        except Exception as e:
            return f"Error getting diff: {e}"

    def list_checkpoints(self) -> list[dict[str, str]]:
        """List all checkpoints in this shadow repo."""
        result = self._git("log", "--oneline", "--format=%H|%s|%ci")
        if result.returncode != 0:
            return []

        entries = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("|", 2)
            if len(parts) == 3:
                sha, msg, date = parts
                # Parse checkpoint metadata from message
                meta = msg.split(":", 3) if msg.startswith("checkpoint:") else [msg]
                entries.append({
                    "sha": sha,
                    "message": msg,
                    "date": date,
                    "chat_id": meta[1] if len(meta) > 1 else "",
                    "message_id": meta[2] if len(meta) > 2 else "",
                    "tool_name": meta[3] if len(meta) > 3 else "",
                })
        return entries


# --- Module-level cache ---
_managers: dict[str, CheckpointManager] = {}


def get_checkpoint_manager(project_root: str) -> CheckpointManager:
    """Get or create a CheckpointManager for the given project root."""
    resolved = str(Path(project_root).resolve())
    if resolved not in _managers:
        _managers[resolved] = CheckpointManager(resolved)
    return _managers[resolved]
