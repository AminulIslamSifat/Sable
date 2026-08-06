"""Integrated terminal: PTY over WebSocket.

Spawns an interactive shell (fish by default) inside a pseudo-terminal and
bridges it to a WebSocket so the browser (xterm.js) gets a real reactive
terminal. Uses ``os.forkpty()`` — the same ``forkpty``/``login_tty`` mechanism
node-pty/VS Code rely on — so the PTY becomes the shell's controlling terminal,
which interactive shells like fish require.

Messages are JSON:

  client -> server : {"type": "input",  "data": "..."}
                     {"type": "resize", "rows": N, "cols": N}
  server -> client : {"type": "output", "data": "..."}
                     {"type": "exit",   "code": N}
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
import signal
import struct
import termios

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from server.auth import AUTH_TOKEN
from server.utils import logger

router = APIRouter()

# ---------------------------------------------------------------------------
# Fish 4.x sends terminal capability probes on startup that older xterm.js
# versions don't answer.  We intercept them server-side and respond
# immediately so the shell doesn't hang.
# ---------------------------------------------------------------------------

_XTGETTCAP_RE = re.compile(rb'\x1bP\+q([0-9a-fA-F]+)\x1b\\')


def _respond_to_probes(fd: int, data: bytes) -> None:
    """Answer terminal capability queries that xterm.js 4.x doesn't handle."""
    responses: list[bytes] = []

    # Kitty keyboard protocol query → not supported
    if b'\x1b[?u' in data:
        responses.append(b'\x1b[?0u')

    # XTVERSION query → identify as xterm
    if b'\x1b[>0q' in data:
        responses.append(b'\x1bP>|xterm(256)\x1b\\')

    # OSC 11 background-colour query → dark bg
    if b'\x1b]11;?' in data:
        responses.append(b'\x1b]11;rgb:1e1e/1e1e/1e1e\x1b\\')

    # NOTE: do NOT answer XTGETTCAP (+q) queries — fish leaks our replies
    # into the command line as typed text.  It degrades gracefully without them.

    # DA1 (Primary Device Attributes) query → VT220 with ANSI colour
    if b'\x1b[0c' in data or b'\x1b[c' in data:
        responses.append(b'\x1b[?1;2c')

    # Cursor Position Report
    if b'\x1b[6n' in data:
        responses.append(b'\x1b[1;1R')

    if responses:
        try:
            os.write(fd, b''.join(responses))
        except OSError:
            pass


# ---------------------------------------------------------------------------


def _pick_shell() -> str:
    override = os.environ.get("SABLE_TERM_SHELL")
    if override and os.path.exists(override):
        return override
    for cand in ("/usr/bin/fish", "/bin/fish", "/bin/bash", "/bin/sh"):
        if os.path.exists(cand):
            return cand
    return "/bin/sh"


SHELL = _pick_shell()


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


@router.websocket("/ws/terminal")
async def terminal_ws(ws: WebSocket) -> None:
    if ws.query_params.get("token", "") != AUTH_TOKEN:
        await ws.close(code=4001)
        return
    await ws.accept()
    cwd_param = ws.query_params.get("cwd", "")

    env = os.environ.copy()
    env.update({"TERM": "xterm-256color", "COLORTERM": "truecolor", "SHELL": SHELL})
    home = os.path.expanduser("~")

    pid, master_fd = os.forkpty()
    if pid == 0:  # ---- child ----
        # Start in the requested dir (IDE folder), else inherit server cwd.
        try:
            if cwd_param and os.path.isdir(cwd_param):
                os.chdir(cwd_param)
        except OSError:
            pass
        try:
            os.execvpe(SHELL, [SHELL], env)
        except Exception:
            os._exit(127)

    # ---- parent ----
    logger.info("terminal: spawned %s pid=%d", SHELL, pid)
    _set_winsize(master_fd, 24, 80)

    fl = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

    loop = asyncio.get_running_loop()

    async def _safe_send(payload: dict) -> None:
        try:
            await ws.send_text(json.dumps(payload))
        except Exception:
            pass

    def _on_readable() -> None:
        try:
            data = os.read(master_fd, 65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            try:
                loop.remove_reader(master_fd)
            except Exception:
                pass
            return
        if not data:
            try:
                loop.remove_reader(master_fd)
            except Exception:
                pass
            return

        # Answer fish's terminal probes before forwarding to the client
        _respond_to_probes(master_fd, data)

        asyncio.ensure_future(
            _safe_send({"type": "output", "data": data.decode("utf-8", "replace")})
        )

    loop.add_reader(master_fd, _on_readable)

    async def _wait_proc() -> int:
        _, status = await loop.run_in_executor(None, os.waitpid, pid, 0)
        if os.WIFEXITED(status):
            code = os.WEXITSTATUS(status)
        elif os.WIFSIGNALED(status):
            code = -os.WTERMSIG(status)
        else:
            code = -1
        logger.info("terminal: process pid=%d exited code=%s", pid, code)
        await asyncio.sleep(0.15)
        return code

    proc_task = asyncio.create_task(_wait_proc())

    async def _reader() -> None:
        while True:
            msg = await ws.receive_text()
            try:
                data = json.loads(msg)
            except json.JSONDecodeError:
                continue
            kind = data.get("type")
            if kind == "input":
                text = data.get("data", "")
                if text:
                    try:
                        os.write(master_fd, text.encode("utf-8"))
                    except OSError:
                        return
            elif kind == "resize":
                rows, cols = int(data.get("rows", 24)), int(data.get("cols", 80))
                _set_winsize(master_fd, rows, cols)
                try:
                    os.killpg(os.getpgid(pid), signal.SIGWINCH)
                except (OSError, ProcessLookupError):
                    pass

    reader_task = asyncio.create_task(_reader())

    try:
        done, _ = await asyncio.wait(
            {reader_task, proc_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for t in done:
            if t.cancelled():
                continue
            exc = t.exception()
            if exc is not None:
                raise exc
        if proc_task in done:
            await _safe_send({"type": "exit", "code": proc_task.result()})
    except WebSocketDisconnect:
        logger.info("terminal: client disconnected")
    except Exception as exc:  # noqa: BLE001
        logger.debug("terminal ws error: %s: %s", type(exc).__name__, exc)
    finally:
        reader_task.cancel()
        proc_task.cancel()
        try:
            loop.remove_reader(master_fd)
        except Exception:
            pass
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        try:
            os.close(master_fd)
        except OSError:
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
