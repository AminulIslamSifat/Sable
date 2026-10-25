"""Run a bundled Python script by path — correct in source AND frozen builds.

PyInstaller sets ``sys.executable`` to the frozen binary, not an interpreter.
So the common idiom

    subprocess.run([sys.executable, "script.py", ...])

does not run the script in a packaged app — it relaunches the whole server.
The caller then gets the startup banner on stdout instead of JSON, and the
child tries to bind the inherited ``SABLE_PORT``, dying with ``Errno 98``.

``script_cmd()`` returns an argv that works either way:

    source build : [python,  script, *args]
    frozen build : [binary,  --run-script, script, *args]

``server.py`` handles ``--run-script`` with ``runpy`` before any startup work,
so the child never touches the port, the scheduler, or the log system.

``child_env()`` also strips the server bind variables from the environment, so
a child that itself starts a server gets a fresh port instead of fighting the
parent for one.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Variables that describe *this* server process and must not leak into a
# child: the port is already bound, and the host is the parent's choice.
_BIND_VARS = ("SABLE_PORT", "SABLE_HOST")


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def script_cmd(script: str | Path, *args: object) -> list[str]:
    """Build an argv that runs *script* with *args*.

    Works unchanged in a source checkout and inside a frozen build.
    """
    script = str(script)
    tail = [str(a) for a in args]
    if is_frozen():
        return [sys.executable, "--run-script", script, *tail]
    return [sys.executable, script, *tail]


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for a spawned script: parent's, minus the bind vars."""
    env = {k: v for k, v in os.environ.items() if k not in _BIND_VARS}
    # A child must never inherit a half-initialised bytecode setting.
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    # Bundled scripts import `engine.*` (for PERSISTENT_ROOT). In a frozen
    # build that resolves out of the PYZ. In a source checkout the child is
    # started as `python path/to/script.py`, which puts the *script's own
    # directory* on sys.path[0] — so `engine` is not importable and the child
    # dies with ModuleNotFoundError before it prints anything. Supplying the
    # project root here fixes every bundled script at once.
    if not is_frozen():
        root = str(Path(__file__).resolve().parent.parent)
        prev = env.get("PYTHONPATH")
        env["PYTHONPATH"] = root + ((os.pathsep + prev) if prev else "")

    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


def run_script_by_path(script: str | Path, argv: list[str] | None = None) -> int:
    """Execute *script* as ``__main__`` in this process. Returns an exit code.

    Called from ``server.py`` when it sees ``--run-script``. Kept here so the
    entry point stays a three-line branch.
    """
    import runpy

    path = Path(script)
    if not path.is_file():
        print(f"sable: no such script: {path}", file=sys.stderr)
        return 2

    # Drop the bind vars so anything this script starts gets its own port.
    for var in _BIND_VARS:
        os.environ.pop(var, None)

    saved_argv = sys.argv
    sys.argv = [str(path), *(argv or [])]
    try:
        runpy.run_path(str(path), run_name="__main__")
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 1
    finally:
        sys.argv = saved_argv
    return 0
