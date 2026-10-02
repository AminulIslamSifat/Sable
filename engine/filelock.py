"""Cross-platform advisory file locking.

Replaces bare ``import fcntl`` (POSIX-only) at every call site. POSIX takes an
exclusive ``flock``; Windows uses ``msvcrt.locking`` on a one-byte range. Both
lock the whole sidecar file, so the existing lock-file pattern is unchanged.
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator, Union

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    import msvcrt
else:
    import fcntl

_PathLike = Union[str, os.PathLike]


def lock(fh: IO[str]) -> None:
    """Take an exclusive lock on an open file handle (blocking)."""
    if _IS_WINDOWS:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
    else:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)


def unlock(fh: IO[str]) -> None:
    """Release a lock previously taken by :func:`lock`."""
    if _IS_WINDOWS:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextmanager
def exclusive_lock(path: _PathLike) -> Iterator[None]:
    """Open ``path``, hold an exclusive lock, yield, then release and close."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fh = open(p, "w", encoding="utf-8")
    try:
        lock(fh)
        try:
            yield
        finally:
            unlock(fh)
    finally:
        fh.close()
