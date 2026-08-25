"""POSIX PTY backend using forkpty().

Extracted from the original terminal.py so the WebSocket handler can be
platform-agnostic.  This module is only imported on non-Windows platforms.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import signal
import struct
import termios
from typing import NamedTuple

from server.utils import logger


class PtyHandle(NamedTuple):
    pid: int
    fd: int


class PosixPtySession:
    """Context-manager wrapping a POSIX forkpty session."""

    def __init__(self, shell: str, env: dict[str, str], cwd: str | None = None,
                 rows: int = 24, cols: int = 80) -> None:
        self._shell = shell
        self._env = env
        self._cwd = cwd
        self._rows = rows
        self._cols = cols
        self._pid: int = 0
        self._fd: int = -1

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> PtyHandle:
        """Fork a child with a PTY and exec the shell.  Returns (pid, master_fd)."""
        pid, master_fd = os.forkpty()
        if pid == 0:  # child
            try:
                if self._cwd and os.path.isdir(self._cwd):
                    os.chdir(self._cwd)
            except OSError:
                pass
            try:
                os.execvpe(self._shell, [self._shell], self._env)
            except Exception:
                os._exit(127)

        self._pid = pid
        self._fd = master_fd
        self.resize(self._rows, self._cols)

        # Set non-blocking so asyncio add_reader works
        fl = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

        logger.info("terminal: spawned %s pid=%d", self._shell, pid)
        return PtyHandle(pid=pid, fd=master_fd)

    async def stop(self) -> None:
        """Terminate the child process tree and close the master fd."""
        pid, fd = self._pid, self._fd
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
            self._fd = -1

        if pid > 0:
            await asyncio.sleep(0.1)
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
            await asyncio.sleep(0.1)
            try:
                os.kill(pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            try:
                os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                pass
            self._pid = 0

    # -- I/O -----------------------------------------------------------------

    def read(self, fd: int, n: int = 65536) -> bytes:
        """Read up to *n* bytes from the master fd.  Returns b'' on EOF/error."""
        try:
            return os.read(fd, n)
        except (BlockingIOError, InterruptedError):
            return b""
        except OSError:
            return b""

    def write(self, fd: int, data: bytes) -> bool:
        """Write *data* to the master fd.  Returns False on failure."""
        try:
            os.write(fd, data)
            return True
        except OSError:
            return False

    # -- resize / signals ----------------------------------------------------

    def resize(self, rows: int, cols: int) -> None:
        """Set the PTY window size via TIOCSWINSZ ioctl."""
        try:
            fcntl.ioctl(
                self._fd, termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, 0, 0),
            )
        except OSError:
            pass

    def send_winch(self) -> None:
        """Send SIGWINCH to the child's process group so it re-reads winsize."""
        try:
            os.killpg(os.getpgid(self._pid), signal.SIGWINCH)
        except (OSError, ProcessLookupError):
            pass

    # -- wait ----------------------------------------------------------------

    async def wait(self) -> int:
        """Block until the child exits.  Returns exit code."""
        loop = asyncio.get_running_loop()
        _, status = await loop.run_in_executor(None, os.waitpid, self._pid, 0)
        if os.WIFEXITED(status):
            return os.WEXITSTATUS(status)
        if os.WIFSIGNALED(status):
            return -os.WTERMSIG(status)
        return -1
