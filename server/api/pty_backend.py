"""Cross-platform PTY backend abstraction.

Provides a unified interface for spawning interactive shells in a
pseudo-terminal.  On POSIX (Linux/macOS) it uses the native forkpty()
mechanism.  On Windows it delegates to ConPTY via the ``pywinpty`` package
(requires Windows 10 1809+).

Public API:
    IS_WINDOWS      – bool, True on Windows
    pick_shell()    – returns path/name of best available shell
    PtySession      – context-manager wrapping a live PTY session
"""

from __future__ import annotations

import os

# Single source of truth lives in engine.platform_paths — re-export here so
# terminal code has one import site without duplicating the platform logic.
from engine.platform_paths import IS_WINDOWS, pick_shell as _pick_shell


def pick_shell() -> str:
    """Return the best available interactive shell for this platform.

    Honors the SABLE_TERM_SHELL override before falling back to auto-detection.
    """
    override = os.environ.get("SABLE_TERM_SHELL")
    if override and os.path.exists(override):
        return override
    return _pick_shell()


if IS_WINDOWS:
    from .pty_win32 import WinPtySession as PtySession
else:
    from .pty_posix import PosixPtySession as PtySession

__all__ = ["IS_WINDOWS", "pick_shell", "PtySession"]
