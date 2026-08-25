"""Integrated terminal: PTY over WebSocket.

Spawns an interactive shell inside a pseudo-terminal and bridges it to a
WebSocket so the browser (xterm.js) gets a real reactive terminal.

On POSIX (Linux/macOS) uses forkpty().  On Windows uses ConPTY via pywinpty
(requires Windows 10 1809+).  The platform-specific logic lives in
``server.api.pty_backend`` / ``pty_posix`` / ``pty_win32``.

Messages are JSON:

  client -> server : {"type": "input",  "data": "..."}
                     {"type": "resize", "rows": N, "cols": N}
  server -> client : {"type": "output", "data": "..."}
                     {"type": "exit",   "code": N}
"""

from __future__ import annotations

import asyncio
import json
import os
import re

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

import server.auth as _auth_mod
from server.api.pty_backend import IS_WINDOWS, PtySession, pick_shell
from server.utils import logger

router = APIRouter()

# ---------------------------------------------------------------------------
# Fish 4.x sends terminal capability probes on startup that older xterm.js
# versions don't answer.  We intercept them server-side and respond
# immediately so the shell doesn't hang.
# Only relevant on POSIX — ConPTY/pwsh don't send these probes.
# ---------------------------------------------------------------------------

_XTGETTCAP_RE = re.compile(rb'\x1bP\+q([0-9a-fA-F]+)\x1b\\')


def _respond_to_probes(session: PtySession, fd: int, data: bytes) -> None:
    """Answer terminal capability queries that xterm.js 4.x doesn't handle."""
    if IS_WINDOWS:
        return  # ConPTY shells don't send these probes

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
        session.write(fd, b''.join(responses))


# ---------------------------------------------------------------------------

SHELL = pick_shell()


@router.websocket("/ws/terminal")
async def terminal_ws(ws: WebSocket) -> None:
    if ws.query_params.get("token", "") != _auth_mod.AUTH_TOKEN:
        await ws.close(code=4001)
        return
    await ws.accept()
    cwd_param = ws.query_params.get("cwd", "")

    env = os.environ.copy()
    env.update({"TERM": "xterm-256color", "COLORTERM": "truecolor", "SHELL": SHELL})

    session = PtySession(shell=SHELL, env=env, cwd=cwd_param or None)
    handle = await session.start()
    pid, fd = handle.pid, handle.fd

    loop = asyncio.get_running_loop()

    async def _safe_send(payload: dict) -> None:
        try:
            await ws.send_text(json.dumps(payload))
        except Exception:
            pass

    # -- Output reader -------------------------------------------------------
    # On POSIX we use loop.add_reader for zero-copy async I/O on the master fd.
    # On Windows, ConPTY doesn't expose an fd so we poll in a thread.

    _reader_running = True

    if not IS_WINDOWS:
        def _on_readable() -> None:
            data = session.read(fd)
            if not data:
                try:
                    loop.remove_reader(fd)
                except Exception:
                    pass
                return
            _respond_to_probes(session, fd, data)
            asyncio.ensure_future(
                _safe_send({"type": "output", "data": data.decode("utf-8", "replace")})
            )

        loop.add_reader(fd, _on_readable)
    else:
        async def _win_reader() -> None:
            """Poll ConPTY output in a background task."""
            while _reader_running:
                data = await loop.run_in_executor(None, session.read, fd, 65536)
                if data:
                    await _safe_send({"type": "output", "data": data.decode("utf-8", "replace")})
                else:
                    await asyncio.sleep(0.02)

        win_reader_task = asyncio.create_task(_win_reader())

    # -- Process waiter ------------------------------------------------------

    async def _wait_proc() -> int:
        code = await session.wait()
        logger.info("terminal: process pid=%d exited code=%s", pid, code)
        await asyncio.sleep(0.15)
        return code

    proc_task = asyncio.create_task(_wait_proc())

    # -- Input handler -------------------------------------------------------

    async def _input_reader() -> None:
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
                    session.write(fd, text.encode("utf-8"))
            elif kind == "resize":
                rows = int(data.get("rows", 24))
                cols = int(data.get("cols", 80))
                session.resize(rows, cols)
                session.send_winch()

    reader_task = asyncio.create_task(_input_reader())

    # -- Main wait -----------------------------------------------------------

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
        _reader_running = False
        reader_task.cancel()
        proc_task.cancel()
        if not IS_WINDOWS:
            try:
                loop.remove_reader(fd)
            except Exception:
                pass
        else:
            win_reader_task.cancel()
        await session.stop()
