"""first_run — seed bundled defaults into the persistent root on first launch.

Why this exists
---------------
In a source checkout, ``PERSISTENT_ROOT`` *is* the project root, so
``instruction/``, ``Brain/`` and friends are already there and everything
just works.

In the packaged app the Electron shell sets ``SABLE_PERSISTENT_ROOT`` to
``<userData>/data`` so user state survives upgrades. That directory starts
empty, while the real defaults ship inside the PyInstaller bundle at
``_internal/instruction``. Nothing copied them across, so
``personas.py`` globbed an empty directory and every persona (Maria, Coder,
Designer, Researcher, Writer, Study, Reviewer) vanished from the UI.

The rule is copy-if-absent: an existing file is NEVER overwritten, so a user
who edited Maria.md keeps their edits across upgrades. Only missing files are
filled in from the bundle.
"""

from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path

logger = logging.getLogger("sable")

# Directories that hold shipped defaults and must exist in PERSISTENT_ROOT.
# `instruction` carries the personas; `Brain` the starter memory files.
_SEED_DIRS = ("instruction", "Brain", "skills")

# Never copy editor scratch even if it slipped into the bundle.
_SKIP_DIRS = {"__pycache__", ".editor_tools_backups", ".sable_backups"}
_SKIP_SUFFIXES = (".pyc", ".pyo", ".bak", ".orig", ".rej")

_seeded = False


def _bundle_root() -> Path | None:
    """Where the shipped defaults live.

    Frozen (PyInstaller): datas land in ``sys._MEIPASS`` — i.e.
    ``_internal/`` next to the executable — so that is the source root.

    Source checkout: the project root, two levels up from this file.
    """
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent / "_internal"
    return Path(__file__).resolve().parent.parent


def _is_junk(p: Path) -> bool:
    if any(part in _SKIP_DIRS for part in p.parts):
        return True
    if p.name.endswith(_SKIP_SUFFIXES) or ".bak" in p.name:
        return True
    return False


def _seed_dir(src_root: Path, dest_root: Path, name: str) -> int:
    """Copy ``src_root/name`` into ``dest_root/name``, never clobbering.

    Returns the number of files copied.
    """
    src = src_root / name
    dest = dest_root / name
    if not src.is_dir():
        return 0

    copied = 0
    for f in src.rglob("*"):
        if not f.is_file() or _is_junk(f):
            continue
        rel = f.relative_to(src)
        target = dest / rel
        if target.exists():
            continue  # user data wins, always
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)
            copied += 1
        except OSError as exc:
            logger.warning("first_run: could not seed %s: %s", target, exc)
    return copied


def seed_defaults(persistent_root: Path) -> None:
    """Populate an empty persistent root with the bundled defaults.

    Idempotent and safe to call on every boot: it only ever adds files that
    are missing, so it is cheap after the first run.
    """
    global _seeded
    if _seeded:
        return
    _seeded = True

    src_root = _bundle_root()
    if src_root is None or not src_root.exists():
        return

    # If the bundle root IS the persistent root (source checkout), there is
    # nothing to do — the files are already in place.
    try:
        if src_root.resolve() == persistent_root.resolve():
            return
    except OSError:
        pass

    total = 0
    for name in _SEED_DIRS:
        n = _seed_dir(src_root, persistent_root, name)
        if n:
            logger.info("first_run: seeded %d file(s) into %s/", n, name)
        total += n

    if total:
        logger.info("first_run: %d default file(s) seeded into %s", total, persistent_root)


def ensure_personas(persistent_root: Path) -> None:
    """Guarantee ``instruction/`` has at least the shipped personas.

    A lighter check used as a safety net: if the directory is missing or holds
    no ``*.md`` files, run the full seed.
    """
    instr = persistent_root / "instruction"
    try:
        has_md = instr.is_dir() and any(instr.glob("*.md"))
    except OSError:
        has_md = False
    if not has_md:
        global _seeded
        _seeded = False  # allow the full pass to run again
        seed_defaults(persistent_root)