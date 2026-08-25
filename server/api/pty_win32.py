"""Windows PTY backend using ConPTY via pywinpty.

Requires Windows 10 1809+ and the ``pywinpty`` package (>= 2.0).
ConPTY provides a real pseudo-console so interactive shells like pwsh,
cmd, and even WSL bash behave correctly with full ANSI/VT support.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from typing import NamedTuple

from server.utils import logger


class PtyHandle(NamedTuple):
    pid: int
    fd: int  # unused on Windows; kept for API parity with POSIX backend


class WinPtySession:
    """Context-manager wrapping a ConPTY session via pywinpty."""

    def __init__(self, shell: str, env: dict[str, str], cwd: str | None = None,
                 rows: int = 24, cols: int = 80) -> None:
        self._shell = shell
        self._env = env
        self._cwd = cwd or os.getcwd()
        self._rows = rows
        self._cols = cols
        self._pty = None  # winpty.Pty instance
        self._pid: int = 0

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> PtyHandle:
        """Spawn the shell inside a ConPTY.  Returns (pid, -1)."""
        from winpty import Pty  # type: ignore[import-untyped]

        pty = Pty(self._cols, self._rows)
        pty.spawn(self._shell, cwd=self._cwd, env=self._env)
        self._pty = pty
        self._pid = pty.pid
        logger.info("terminal: spawned %s pid=%d (ConPTY)", self._shell, self._pid)
        return PtyHandle(pid=pty.pid, fd=-1)

    async def stop(self) -> None:
        """Kill the process tree and release the ConPTY handle."""
        if self._pty is not None:
            try:
                self._pty.close()
            except Exception:
                pass
            self._pty = None

        if self._pid > 0:
            await asyncio.sleep(0.1)
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(self._pid)],
                    capture_output=True, timeout=5,
                )
            except Exception:
                pass
            self._pid = 0

    # -- I/O -----------------------------------------------------------------

    def read(self, _fd: int, n: int = 65536) -> bytes:
        """Read output from the ConPTY.  Returns b'' if nothing available."""
        if self._pty is None:
            return b""
        try:
            data = self._pty.read(n)
            if isinstance(data, str):
                return data.encode("utf-8", errors="replace")
            return data if data else b""
        except Exception:
            return b""

    def write(self, _fd: int, data: bytes) -> bool:
        """Write input to the ConPTY.  Returns False on failure."""
        if self._pty is None:
            return False
        try:
            text = data.decode("utf-8", errors="replace")
            self._pty.write(text)
            return True
        except Exception:
            return False

    # -- resize / signals ----------------------------------------------------

    def resize(self, rows: int, cols: int) -> None:
        """Resize the ConPTY buffer."""
        if self._pty is not None:
            try:
                self._pty.set_size(cols, rows)  # pywinpty takes (cols, rows)
            except Exception:
                pass

    def send_winch(self) -> None:
        """No-op on Windows — ConPTY handles resize notifications internally."""

    # -- wait ----------------------------------------------------------------

    async def wait(self) -> int:
        """Block until the child exits.  Returns exit code."""
        if self._pty is None:
            return -1
        loop = asyncio.get_running_loop()
        try:
            code = await loop.run_in_executor(None, self._pty.get_exit_status)
            return code if code is not None else -1
        except Exception:
            return -1
