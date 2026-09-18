#!/usr/bin/env python3
"""Self-test for the pure helpers in server/api/routes/update.py.

Run:  python3 tools/test_update_helpers.py
Exits non-zero on failure. No server, no network.
"""

from __future__ import annotations

import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Stub out FastAPI deps so we can import the module standalone.
import types
for mod_name in ("server.utils", "server.api.dependencies"):
    pass

try:
    from server.api.routes import update as U
except Exception as exc:  # pragma: no cover
    print(f"✖ import failed: {exc}")
    sys.exit(1)

FAILED = 0

def check(label: str, cond: bool) -> None:
    global FAILED
    print(f"  {'✓' if cond else '✖'} {label}")
    if not cond:
        FAILED = 1


print("version logic:")
check("1.9.0 < 1.9.1", U._compare_versions("1.9.0", "1.9.1"))
check("1.9.1 not < 1.9.1", not U._compare_versions("1.9.1", "1.9.1"))
check("1.10.0 > 1.9.9", U._compare_versions("1.9.9", "1.10.0"))
check("extract from 'v2.0.3'", U._extract_version("v2.0.3") == "2.0.3")
check("extract from 'Release 2.0.3'", U._extract_version("Release 2.0.3") == "2.0.3")

print("protected paths:")
check("system/ preserved", U._is_protected("system/sable.db"))
check("output/ preserved", U._is_protected("output/notes/x.md"))
check("Brain/Memory.json preserved", U._is_protected("Brain/Memory.json"))
check("Maria.md preserved", U._is_protected("instruction/Maria.md"))
check(".venv/ preserved", U._is_protected(".venv/lib/x.py"))
check("engine/service.py NOT protected", not U._is_protected("engine/service.py"))
check("Brain/Memory.json.example NOT protected", not U._is_protected("Brain/Memory.json.example"))

print("asset picking:")
release = {
    "tag_name": "v1.9.1",
    "assets": [
        {"name": "sable-1.9.1-linux.zip", "browser_download_url": "https://x/linux.zip", "size": 100},
        {"name": "sable-1.9.1-windows.zip", "browser_download_url": "https://x/win.zip", "size": 200},
        {"name": "sable-1.9.1-macos.zip", "browser_download_url": "https://x/mac.zip", "size": 150},
    ],
}
# force platform by monkeypatching
for tag, expect in (("linux", "sable-1.9.1-linux.zip"),
                    ("windows", "sable-1.9.1-windows.zip"),
                    ("macos", "sable-1.9.1-macos.zip")):
    U._platform_tag = lambda t=tag: t
    a = U._pick_asset(release, "1.9.1")
    check(f"{tag} -> {expect}", a is not None and a["name"] == expect)

U._platform_tag = lambda: "linux"
check("missing version -> None", U._pick_asset(release, "9.9.9") is None)

# Regression: a stray asset for another version must NOT be picked just
# because it matches the platform.
stray = {"tag_name": "v2.0.0", "assets": [{"name": "sable-1.9.1-linux.zip", "browser_download_url": "u", "size": 1}]}
check("wrong-version asset rejected", U._pick_asset(stray, "2.0.0") is None)
# But a version+platform match with an odd prefix IS accepted.
oddb = {"tag_name": "v2.0.0", "assets": [{"name": "sable-2.0.0-linux.zip", "browser_download_url": "u", "size": 1}]}
check("version+platform fallback works", (U._pick_asset(oddb, "2.0.0") or {}).get("name") == "sable-2.0.0-linux.zip")

print("zip extraction safety:")
with tempfile.TemporaryDirectory() as td:
    tdp = Path(td)
    zpath = tdp / "evil.zip"
    dest = tdp / "dest"
    dest.mkdir()
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("engine/ok.py", "print('ok')\n")
        zf.writestr("../escaped.py", "pwned\n")
        zf.writestr("system/sable.db", "SECRET\n")
        zf.writestr("Brain/Memory.json", '{"personal": true}\n')
        zf.writestr("Brain/Memory.json.example", "{}\n")

    n = U._safe_extract(zpath, dest)
    check("extracted the safe file", (dest / "engine/ok.py").exists())
    check("did NOT escape via ../", not (tdp / "escaped.py").exists())
    check("did NOT write system/", not (dest / "system/sable.db").exists())
    check("did NOT overwrite Brain/Memory.json", not (dest / "Brain/Memory.json").exists())
    check("DID write Memory.json.example", (dest / "Brain/Memory.json.example").exists())
    check("count == 2 (ok + example)", n == 2)

print("snapshot + rollback:")
with tempfile.TemporaryDirectory() as td:
    tdp = Path(td)
    install = tdp / "install"
    stage = tdp / "stage"
    backup = tdp / "backup"
    install.mkdir()
    stage.mkdir()

    # Live install state
    (install / "engine").mkdir()
    (install / "engine" / "service.py").write_text("OLD service\n")
    (install / "engine" / "untouched.py").write_text("KEEP ME\n")
    (install / "server").mkdir()
    (install / "server" / "app.py").write_text("OLD app\n")
    (install / "system").mkdir()
    (install / "system" / "sable.db").write_text("MY DATA\n")

    # Incoming package
    (stage / "engine").mkdir()
    (stage / "engine" / "service.py").write_text("NEW service\n")
    (stage / "engine" / "brand_new.py").write_text("NEW file\n")
    (stage / "server").mkdir()
    (stage / "server" / "app.py").write_text("NEW app\n")
    (stage / "system").mkdir()
    (stage / "system" / "sable.db").write_text("PACKAGE DATA\n")

    planned = U._plan_overlay(stage, install)
    check("plan excludes protected system/", "system/sable.db" not in planned)
    check("plan includes engine/service.py", "engine/service.py" in planned)
    check("plan includes new file", "engine/brand_new.py" in planned)
    check("plan count == 3", len(planned) == 3)

    journal = U._backup_files(planned, install, backup)
    check("journal len == 3", len(journal) == 3)
    overwritten = [e for e in journal if e["existed"]]
    new_files = [e for e in journal if not e["existed"]]
    check("2 pre-existing files backed up", len(overwritten) == 2)
    check("1 new file flagged existed=False", len(new_files) == 1)
    check("backup dir has engine/service.py", (backup / "engine" / "service.py").exists())

    # Simulate the overlay
    U._overlay_tree(stage, install)
    check("overlay wrote new service.py", "NEW service" in (install / "engine" / "service.py").read_text())
    check("overlay created brand_new.py", (install / "engine" / "brand_new.py").exists())
    check("overlay left system/sable.db alone", "MY DATA" in (install / "system" / "sable.db").read_text())

    # ── Roll back ──
    restored, removed = U._restore_files(journal, install)
    check("restored 2 files", restored == 2)
    check("removed 1 new file", removed == 1)
    check("service.py back to OLD", "OLD service" in (install / "engine" / "service.py").read_text())
    check("app.py back to OLD", "OLD app" in (install / "server" / "app.py").read_text())
    check("brand_new.py removed", not (install / "engine" / "brand_new.py").exists())
    check("user data intact", "MY DATA" in (install / "system" / "sable.db").read_text())
    check("untouched file survived", (install / "engine" / "untouched.py").exists())

print("rollback edge cases:")
with tempfile.TemporaryDirectory() as td:
    tdp = Path(td)
    check("empty journal restores cleanly", U._restore_files([], tdp) == (0, 0))

    # A file that existed, was overwritten, but backup is missing on disk
    install = tdp / "i"; install.mkdir()
    (install / "x.py").write_text("NEW\n")
    bad_journal = [{"rel": "x.py", "existed": True, "backup": str(tdp / "gone.py")}]
    r, rm = U._restore_files(bad_journal, install)
    check("missing backup doesn't crash", r == 0 and rm == 0)

    # new-file entry where the file was already deleted
    gone_journal = [{"rel": "never.py", "existed": False, "backup": None}]
    r2, rm2 = U._restore_files(gone_journal, install)
    check("absent new file is a no-op", r2 == 0 and rm2 == 0)

print()
if FAILED:
    print("✖ FAILED")
    sys.exit(1)
print("✅ all helper tests passed")
