# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Sable — bundles the server + web UI only.
# Runtime state (system/, browser profiles, models, DB) is created fresh on the
# user's machine under app.getPath("userData")/data — never shipped.

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).resolve().parent

# ── what we ship: code + static UI, not user data ───────────────
PROJECT_DIRS = [
    "web",           # the entire frontend (87 MB)
    "Brain",         # prompts / persona
    "skills",        # skill instructions
    "tools",         # tool definitions
    "instruction",   # agent instructions
    "connectors",    # API clients
    "documentation", # reference docs
]

# Editor/agent scratch that must never ship: auto-backups, .bak copies,
# compiled caches, and the vendored opencode binary (178 MB, referenced by a
# comment only — the Python client speaks the HTTP API and never execs it).
SKIP_DIRS = {
    "__pycache__", ".editor_tools_backups", ".sable_backups",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules",
}
SKIP_SUFFIXES = (".pyc", ".pyo", ".bak", ".orig", ".rej")
SKIP_BINARIES = {str(ROOT / "connectors" / "opencode" / "bin")}


def _is_junk(path: Path) -> bool:
    if any(part in SKIP_DIRS for part in path.parts):
        return True
    if any(str(path).startswith(b) for b in SKIP_BINARIES):
        return True
    name = path.name
    if name.endswith(SKIP_SUFFIXES):
        return True
    # foo.py.bak.20261023-190459 and friends
    if ".bak" in name:
        return True
    return False


datas = []
for d in PROJECT_DIRS:
    p = ROOT / d
    if not p.exists():
        continue
    for f in p.rglob("*"):
        if not f.is_file():
            continue
        if _is_junk(f):
            continue
        datas.append((str(f), str(f.parent.relative_to(ROOT))))

# ── third-party packages whose data files matter ────────────────
for pkg in [
    "fastapi", "uvicorn", "pydantic", "starlette", "anyio", "httpx",
    "jinja2", "numpy", "curl_cffi", "lxml", "fitz", "pymupdf", "playwright",
    "soundfile", "openpyxl", "pptx", "docx", "pypdf", "bs4",
    "trafilatura", "json_repair", "rank_bm25", "mcp", "telethon",
    "pyaes", "edge_tts", "google", "tenacity", "websockets",
    "aiohttp", "multipart", "oss2", "pymongo", "email_validator",
]:
    try:
        d, _b, _h = collect_all(pkg)
        datas += d
    except Exception:
        pass

# ── hidden imports (our own packages + uvicorn's dynamic bits) ──
hiddenimports = []
for mod in ["engine", "server", "server.api", "server.api.routes",
            "server.database", "tools", "connectors"]:
    try:
        hiddenimports += collect_submodules(mod)
    except Exception:
        pass

hiddenimports += [
    "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto",
    "uvicorn.loops.asyncio", "uvicorn.protocols", "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan", "uvicorn.lifespan.on", "uvicorn.lifespan.off",
    "email.mime.text", "email.mime.multipart",
]

# ── keep the bundle lean: drop optional heavy stacks ────────────
excludes = [
    # GUI toolkits never used by a headless server
    "tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
    # dev tooling
    "test", "unittest", "pydoc", "pydoc_data", "IPython",
    "jupyter", "notebook", "pytest", "_pytest",
    # heavy ML / media stacks not needed for chat
    "torch", "transformers", "tensorflow", "scipy", "pandas",
    "faster_whisper", "ctranslate2", "av",
    "kokoro_onnx", "misaki", "espeakng_loader",
    "matplotlib", "PIL.ImageTk",
]

block_cipher = None

a = Analysis(
    [str(ROOT / "server.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="sable-server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="sable-server",
)
