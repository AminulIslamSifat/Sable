
"""Model download manager — HuggingFace Hub integration with real cancellation.

Downloads run in subprocesses so cancel = kill. No polite stop_event that
gets ignored by a blocking snapshot_download call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from engine.cookbook.state import DownloadTask, get_state

logger = logging.getLogger(__name__)

_REPO_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
_INCLUDE_RE = re.compile(r"^[A-Za-z0-9._\-*?/\[\]]+$")

# Inline script executed in the subprocess — receives config via env vars.
# Writes progress updates to a JSON file so the parent can read real stats.
_DL_SCRIPT = '''\
import os, sys, json, time, threading, fnmatch, re
from pathlib import Path

repo_id = os.environ["_DL_REPO"]
target_dir = os.environ["_DL_DIR"]
filename = os.environ.get("_DL_FILE") or None
include = os.environ.get("_DL_INCLUDE") or None
token = os.environ.get("_DL_TOKEN") or None
progress_file = os.environ["_DL_PROGRESS_FILE"]

# ── Progress state ──
_lock = threading.Lock()
_state = {
    "bytes_downloaded": 0,
    "total_bytes": 0,
    "status": "resolving",
    "updated_at": time.time(),
    "progress": 0.0,
}
_last_write = [0.0]

def _write_progress(force=False):
    now = time.time()
    if not force and now - _last_write[0] < 0.8:
        return
    _last_write[0] = now
    _state["updated_at"] = now
    if _state["total_bytes"] > 0:
        _state["progress"] = min(99.0, (_state["bytes_downloaded"] / _state["total_bytes"]) * 100)
    try:
        Path(progress_file).write_text(json.dumps(_state))
    except OSError:
        pass

# ── Deduplicate split vs single GGUF files ──
# Repos often have BOTH "model-q4_k_m.gguf" (single) AND
# "model-q4_k_m-00001-of-00002.gguf" + "...-00002-of-..." (split).
# A glob like *q4_k_m* matches all → downloads model twice.
_SPLIT_RE = re.compile('^(.+)-([0-9]{5})-of-([0-9]{5})[.]gguf$')

def _dedup_gguf_files(matched_files):
    """Given list of (name, size) tuples matching a glob, remove duplicates.
    If both split parts and a single file exist for same base, keep single."""
    # Group split files by their base name
    split_groups = {}  # base_name -> [(name, size), ...]
    singles = []       # [(name, size), ...]

    for name, size in matched_files:
        m = _SPLIT_RE.match(name)
        if m:
            base = m.group(1)  # e.g. "model-q4_k_m"
            split_groups.setdefault(base, []).append((name, size))
        elif name.endswith('.gguf'):
            # Check if this single file has corresponding splits
            base = name[:-5]  # strip .gguf
            singles.append((name, size, base))
        else:
            singles.append((name, size, None))

    # Determine which split groups have a corresponding single file
    single_bases = {s[2] for s in singles if s[2]}
    result = []
    for name, size, base in singles:
        result.append((name, size))
    for base, parts in split_groups.items():
        if base not in single_bases:
            # No single file exists — keep the splits
            for name, size in parts:
                result.append((name, size))
        # else: single file exists, skip splits (already added above)
    return result

# ── Query total size from HF API ──
def get_total_size():
    from huggingface_hub import HfApi
    api = HfApi(token=token)
    try:
        files = api.list_repo_tree(repo_id, recursive=True)
        matched = []
        for f in files:
            if not hasattr(f, "size") or f.size is None:
                continue
            name = f.rfilename if hasattr(f, "rfilename") else getattr(f, "path", "")
            if filename and name != filename:
                continue
            if include and not fnmatch.fnmatch(name, include):
                continue
            matched.append((name, f.size))
        deduped = _dedup_gguf_files(matched)
        return sum(s for _, s in deduped)
    except Exception:
        return 0

_state["total_bytes"] = get_total_size()
_state["status"] = "downloading"
_write_progress(force=True)

# ── tqdm class: tracks real bytes from download stream ──
class HfProgressTqdm:
    """Drop-in tqdm replacement that reports byte progress to JSON."""
    def __init__(self, *args, **kwargs):
        self.n = 0
        self.total = kwargs.get("total", 0) or 0
        self.desc = kwargs.get("desc", "")
        self.unit = kwargs.get("unit", "B")

    def update(self, n=1):
        self.n += n
        with _lock:
            _state["bytes_downloaded"] += n
            _write_progress()

    def set_description(self, desc=""):
        self.desc = desc

    def set_postfix(self, **kwargs):
        pass

    def set_postfix_str(self, s=""):
        pass

    def set_transfer_postfix_str(self, s=""):
        pass

    def close(self):
        with _lock:
            _write_progress(force=True)

    def refresh(self):
        pass

    def reset(self, total=None):
        self.n = 0
        if total is not None:
            self.total = total

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @property
    def format_dict(self):
        return {"n": self.n, "total": self.total, "unit": self.unit}

from huggingface_hub import snapshot_download, hf_hub_download

try:
    if filename:
        hf_hub_download(
            repo_id=repo_id,
            filename=filename,
            local_dir=target_dir,
            token=token,
            tqdm_class=HfProgressTqdm,
        )
    else:
        # Use explicit deduplicated filenames as allow_patterns instead of
        # glob + ignore_patterns. snapshot_download doesn't reliably skip
        # files via ignore_patterns when allow_patterns glob matches them.
        if include:
            from huggingface_hub import HfApi as _HfApi
            _api = _HfApi(token=token)
            try:
                _files = _api.list_repo_tree(repo_id, recursive=True)
                _matched = []
                for _f in _files:
                    if not hasattr(_f, "size") or _f.size is None:
                        continue
                    _name = _f.rfilename if hasattr(_f, "rfilename") else getattr(_f, "path", "")
                    if fnmatch.fnmatch(_name, include):
                        _matched.append((_name, _f.size))
                _deduped = _dedup_gguf_files(_matched)
                allow_patterns = [n for n, _ in _deduped]
            except Exception:
                allow_patterns = [include]
        else:
            allow_patterns = None
        snapshot_download(
            repo_id=repo_id,
            local_dir=target_dir,
            token=token,
            allow_patterns=allow_patterns,
            tqdm_class=HfProgressTqdm,
        )
    with _lock:
        _state["status"] = "done"
        _state["progress"] = 100.0
        if _state["total_bytes"] > 0:
            _state["bytes_downloaded"] = _state["total_bytes"]
        _write_progress(force=True)
    print("DONE")
except Exception as e:
    with _lock:
        _state["status"] = "error"
        _state["error"] = str(e)[:500]
        _write_progress(force=True)
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)
'''


class DownloadError(Exception):
    pass


class DownloadManager:
    """Manages model downloads via subprocesses. Cancel = kill process."""

    def __init__(self) -> None:
        self._processes: dict[str, subprocess.Popen] = {}
        self._trackers: dict[str, asyncio.Task] = {}

    def validate_repo_id(self, repo_id: str) -> str:
        if not repo_id or not _REPO_ID_RE.match(repo_id):
            raise DownloadError("Invalid repo_id — must be <org>/<name>")
        return repo_id

    def validate_include(self, include: str | None) -> str | None:
        if not include:
            return None
        if not _INCLUDE_RE.match(include):
            raise DownloadError("Invalid include pattern")
        return include

    async def start_download(
        self,
        repo_id: str,
        *,
        include: str | None = None,
        filename: str | None = None,
        hf_token: str | None = None,
        local_dir: str | None = None,
    ) -> DownloadTask:
        """Start a background model download in a killable subprocess."""
        self.validate_repo_id(repo_id)
        self.validate_include(include)

        state = get_state()
        task_id = f"dl-{uuid.uuid4().hex[:8]}"
        target_dir = Path(local_dir) if local_dir else state.models_dir
        target_dir.mkdir(parents=True, exist_ok=True)

        task = DownloadTask(
            id=task_id,
            repo_id=repo_id,
            filename=filename or "",
            include=include or "",
            local_dir=str(target_dir),
            status="downloading",
        )
        state.downloads.append(task)
        state.save()

        # Build env for subprocess
        token = hf_token or state.settings.hf_token or ""
        progress_file = f"/tmp/dl_progress_{task_id}.json"
        env = {
            **os.environ,
            "_DL_REPO": repo_id,
            "_DL_DIR": str(target_dir),
            "_DL_FILE": filename or "",
            "_DL_INCLUDE": include or "",
            "_DL_TOKEN": token,
            "_DL_PROGRESS_FILE": progress_file,
            "HF_HUB_DISABLE_XET": "1",  # Force HTTP — xet bypasses tqdm_class
        }
        self._progress_files = getattr(self, "_progress_files", {})
        self._progress_files[task_id] = progress_file

        proc = subprocess.Popen(
            [sys.executable, "-c", _DL_SCRIPT],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # New process group so we can kill the whole tree
            preexec_fn=os.setsid,
        )
        self._processes[task_id] = proc
        logger.info("Download subprocess started: %s (pid=%d)", repo_id, proc.pid)

        # Async tracker: monitor process + estimate progress
        self._trackers[task_id] = asyncio.create_task(
            self._monitor(task, proc, target_dir)
        )
        return task

    async def _monitor(self, task: DownloadTask, proc: subprocess.Popen, target_dir: Path) -> None:
        """Monitor subprocess via progress file — real bytes, real speed."""
        progress_file = self._progress_files.get(task.id, "")
        last_bytes = 0
        last_time = time.time()

        while proc.poll() is None:
            await asyncio.sleep(1.5)
            if task.status != "downloading":
                break

            # Read progress from subprocess JSON file
            data = self._read_progress_file(progress_file)
            if data:
                task.bytes_downloaded = data.get("bytes_downloaded", 0)
                task.total_bytes = data.get("total_bytes", 0)
                if task.total_bytes > 0:
                    task.progress = min(99.0, (task.bytes_downloaded / task.total_bytes) * 100)

                # Calculate speed
                now = time.time()
                dt = now - last_time
                if dt > 0:
                    db = task.bytes_downloaded - last_bytes
                    if db > 0:
                        task.speed_bps = db / dt
                    last_bytes = task.bytes_downloaded
                    last_time = now

            get_state().save()

        # Process exited — determine outcome
        if task.status == "cancelled":
            self._cleanup_progress_file(task.id)
            return

        returncode = proc.poll()
        if returncode == 0:
            task.status = "done"
            task.progress = 100.0
            task.speed_bps = 0.0
            if task.total_bytes > 0:
                task.bytes_downloaded = task.total_bytes
            logger.info("Download complete: %s", task.repo_id)
        elif returncode in (-9, -15):
            task.status = "cancelled"
            logger.info("Download killed: %s", task.repo_id)
        else:
            stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
            task.status = "failed"
            task.error = stderr[:500] if stderr else f"Process exited with code {returncode}"
            logger.error("Download failed: %s — %s", task.repo_id, task.error)

        task.finished_at = time.time()
        get_state().save()
        self._cleanup_progress_file(task.id)
        self._processes.pop(task.id, None)
        self._trackers.pop(task.id, None)

    def _read_progress_file(self, path: str) -> dict | None:
        """Read progress JSON written by the download subprocess."""
        if not path:
            return None
        try:
            raw = Path(path).read_text()
            return json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return None

    def _cleanup_progress_file(self, task_id: str) -> None:
        """Remove the progress file for a finished download."""
        path = self._progress_files.pop(task_id, "")
        if path:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass

    def cancel_download(self, task_id: str) -> bool:
        """Cancel a download by killing the subprocess. Actually stops it."""
        state = get_state()
        task = state.get_download(task_id)
        if not task or task.status not in ("downloading", "pending"):
            return False

        # Kill the process group (download + any child processes)
        proc = self._processes.get(task_id)
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), 9)  # SIGKILL the whole group
                proc.wait(timeout=5)
                logger.info("Killed download process for %s (pid=%d)", task.repo_id, proc.pid)
            except (ProcessLookupError, OSError):
                # Process already gone
                pass
            except subprocess.TimeoutExpired:
                proc.kill()

        # Cancel the async tracker
        tracker = self._trackers.pop(task_id, None)
        if tracker:
            tracker.cancel()
        self._processes.pop(task_id, None)

        # Delete partial files
        target_dir = Path(task.local_dir)
        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)
            logger.info("Cleaned partial download: %s", target_dir)

        task.status = "cancelled"
        task.finished_at = time.time()
        state.save()
        return True

    @staticmethod
    def _dir_size(path: Path) -> int:
        """Get total size of all files in directory (recursive)."""
        total = 0
        try:
            for f in path.rglob("*"):
                if f.is_file():
                    total += f.stat().st_size
        except OSError:
            pass
        return total

    def list_downloads(self) -> list[DownloadTask]:
        return get_state().downloads

    def scan_cached_models(self) -> list[dict]:
        """Scan the models directory for GGUF files."""
        state = get_state()
        models_dir = state.models_dir
        results = []

        if not models_dir.exists():
            return results

        for gguf in sorted(models_dir.rglob("*.gguf")):
            size_mb = gguf.stat().st_size / (1024 * 1024)
            results.append({
                "path": str(gguf),
                "name": gguf.stem,
                "filename": gguf.name,
                "size_mb": round(size_mb, 1),
                "rel_path": str(gguf.relative_to(models_dir)),
            })

        return results
