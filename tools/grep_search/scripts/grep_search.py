
#!/usr/bin/env python3
"""Grep Search — ripgrep/glob/list_dir with path sandboxing."""

import glob as _glob
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Resolve project root: env var → relative to this script → home fallback
_SCRIPT_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
ALLOWED_ROOTS = [
    Path(os.environ.get("PROJECT_ROOT", str(_SCRIPT_PROJECT_ROOT))).resolve(),
    Path.home().resolve(),
    Path("/tmp").resolve(),
]

_IS_WINDOWS = os.name == "nt"


def _no_window_kwargs() -> dict:
    """Suppress the console window that Windows pops for console-subsystem
    children of a GUI-subsystem parent. No-op on POSIX."""
    if _IS_WINDOWS:
        return {"creationflags": 0x08000000}  # CREATE_NO_WINDOW
    return {}


def _find_ripgrep() -> str | None:
    r"""Locate the ripgrep binary.

    Resolution order (first hit wins):
      1. PATH                                — user already has rg
      2. <project>/system/rg.exe             — bundled by start.ps1
      3. %LOCALAPPDATA%\Programs\ripgrep\rg.exe — common manual install
      4. WinGet package dir                   — winget install BurntSushi.ripgrep

    Returns the full path, or None if ripgrep isn't available anywhere.
    """
    found = shutil.which("rg")
    if found:
        return found

    if not _IS_WINDOWS:
        return None

    candidates: list[str] = []

    # 2. project-local (where start.ps1 drops it)
    candidates.append(str(_SCRIPT_PROJECT_ROOT / "system" / "rg.exe"))

    # 3. standard manual install location
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    if local_appdata:
        candidates.append(os.path.join(local_appdata, "Programs", "ripgrep", "rg.exe"))

    for cand in candidates:
        if os.path.isfile(cand):
            return cand

    # 4. WinGet — glob is kept as a last resort. The running process may hold a
    #    stale PATH after winget installs, so the file can exist but not resolve.
    if local_appdata:
        for pattern in (
            r"Microsoft\WinGet\Packages\BurntSushi.ripgrep*\rg.exe",
            r"Microsoft\WinGet\Links\rg.exe",
            r"Microsoft\WinGet\Packages\BurntSushi.ripgrep*\**\rg.exe",
        ):
            matches = _glob.glob(os.path.join(local_appdata, pattern), recursive=True)
            for m in matches:
                if os.path.isfile(m):
                    return m

    return None

MAX_RESULTS_CAP = 200
MAX_OUTPUT_CHARS = 25_000  # default output budget (~25k chars)
LINE_TRUNCATE = 300        # per-line truncation


def validate_path(raw: str) -> Path:
    """Resolve and verify path is within an allowed root."""
    p = Path(raw).expanduser().resolve()
    for root in ALLOWED_ROOTS:
        try:
            p.relative_to(root)
            return p
        except ValueError:
            continue
    raise PermissionError(f"Path outside allowed roots: {p}")


def _python_grep(pattern: str, root: Path, ignore_case: bool, glob_filter: str | None,
                 excludes: list[str], max_results: int, full_output: bool) -> list[str]:
    """Pure-Python grep fallback.

    Used when ripgrep isn't available AND the system `grep` can't be trusted
    (Windows has no `grep` at all). Slower than rg, but correct and portable.
    """
    try:
        regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        return [f"Error: invalid regex '{pattern}': {exc}"]

    # Mirrors ripgrep's practical behavior closely enough: junk dirs pruned,
    # editor backup trees and .bak files skipped (rg ignores them via .gitignore).
    _DEFAULT_SKIP_DIRS = {
        "vendor", "node_modules", ".git", "__pycache__",
        ".venv", "venv", "dist", "build", ".mypy_cache",
        ".pytest_cache", ".ruff_cache", "target",
        ".editor_tools_backups", ".sable_backups",
    }
    _DEFAULT_SKIP_SUFFIX = (
        ".min.js", ".min.css", ".pyc", ".pyo", ".so", ".dll", ".exe",
        ".bak", ".lock", ".zip", ".gz", ".tar",
    )

    skip_dirs = set(_DEFAULT_SKIP_DIRS)
    skip_globs: list[str] = []
    for excl in excludes:
        if excl.endswith("/"):
            skip_dirs.add(excl.rstrip("/"))
        elif "*" in excl:
            skip_globs.append(excl)
        elif "." in excl:
            skip_globs.append(excl)
        else:
            skip_dirs.add(excl)

    results: list[str] = []
    total_chars = 0
    truncated = False
    files_scanned = 0
    _MAX_FILES = 20_000  # hard stop so a huge tree can't hang the turn

    for dirpath, dirnames, filenames in os.walk(root):
        # prune skip dirs in-place so os.walk doesn't descend into them
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fname in filenames:
            if any(fname.endswith(sfx) for sfx in _DEFAULT_SKIP_SUFFIX):
                continue
            if skip_globs and any(Path(fname).match(g) for g in skip_globs):
                continue
            if glob_filter and not Path(fname).match(glob_filter):
                continue

            files_scanned += 1
            if files_scanned > _MAX_FILES:
                results.append(f"[⚠️ Scanned {_MAX_FILES:,} files, stopping. Narrow the path for full results.]")
                return results

            fpath = Path(dirpath) / fname
            try:
                with fpath.open("r", encoding="utf-8", errors="replace") as fh:
                    for lineno, line in enumerate(fh, 1):
                        if len(results) >= max_results:
                            return results
                        text = line.rstrip("\n")
                        if not regex.search(text):
                            continue
                        if len(text) > LINE_TRUNCATE:
                            text = text[:LINE_TRUNCATE] + "…"
                        entry = f"{fpath}:{lineno}:{text}"
                        if not full_output and total_chars + len(entry) > MAX_OUTPUT_CHARS:
                            truncated = True
                            break
                        total_chars += len(entry)
                        results.append(entry)
                if truncated:
                    results.append(f"[⚠️ Output truncated at {total_chars:,} chars. Use full=\"true\" to get all results.]")
                    return results
            except (OSError, UnicodeError):
                continue

    return results


def cmd_grep(args: dict) -> list[str]:
    pattern = args.get("pattern", "")
    if not pattern:
        return ["Error: 'pattern' is required"]

    search_path = validate_path(args.get("path", str(ALLOWED_ROOTS[0])))
    max_results = min(int(args.get("max_results", 50)), MAX_RESULTS_CAP)
    glob_filter = args.get("glob")
    ignore_case = str(args.get("ignore_case", "")).lower() in ("true", "1", "yes")
    full_output = str(args.get("full", "")).lower() in ("true", "1", "yes")
    exclude_raw = args.get("exclude", "")
    extra_excludes = [e.strip() for e in exclude_raw.split(",") if e.strip()] if exclude_raw else []

    # Try ripgrep first
    rg = _find_ripgrep()

    # No ripgrep? Use the pure-Python walker.
    #
    # NOTE: this deliberately does NOT shell out to `grep -rn`. Windows has no
    # `grep` binary, so the old fallback silently returned zero matches on every
    # Windows box without ripgrep — the worst possible failure mode (looks like
    # "no results" instead of "tool missing"). The Python path works everywhere.
    if not rg:
        py_results = _python_grep(
            pattern, search_path, ignore_case, glob_filter,
            extra_excludes, max_results, full_output,
        )
        if not py_results:
            return [f"No matches found for '{pattern}' in {search_path}"]
        return py_results

    cmd = [rg, "--json", f"--max-count={max_results}"]
    if ignore_case:
        cmd.append("-i")
    # Default exclusions for junk dirs/files
    for excl in ("vendor/", "node_modules/", "*.min.js", "*.min.css", ".git/"):
        cmd.extend(["--glob", f"!{excl}"])
    # User-specified exclusions
    for excl in extra_excludes:
        cmd.extend(["--glob", f"!{excl}"])
    if glob_filter:
        cmd.extend(["--glob", glob_filter])
    cmd.extend([pattern, str(search_path)])

    try:
        proc = subprocess.run(
            cmd, capture_output=True, encoding="utf-8",
            errors="replace", timeout=30, **_no_window_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return ["Error: search timed out after 30s"]
    except Exception as e:
        return [f"Error: {e}"]

    lines: list[str] = []
    total_chars = 0
    truncated = False
    total_match_count = 0

    # Parse ripgrep JSON lines
    for line in proc.stdout.splitlines():
        if len(lines) >= max_results:
            break
        try:
            obj = json.loads(line)
            if obj.get("type") == "match":
                total_match_count += 1
                data = obj["data"]
                path = data["path"]["text"]
                line_num = data["line_number"]
                text = data["lines"]["text"].rstrip("\n")
                if len(text) > LINE_TRUNCATE:
                    text = text[:LINE_TRUNCATE] + "…"
                entry = f"{path}:{line_num}:{text}"
                # Check output budget
                if not full_output and total_chars + len(entry) > MAX_OUTPUT_CHARS:
                    truncated = True
                    break
                total_chars += len(entry)
                lines.append(entry)
        except (json.JSONDecodeError, KeyError):
            continue

    if truncated:
        lines.append(f"[⚠️ Output truncated at {total_chars:,} chars ({len(lines)}/{total_match_count} matches shown). Use full=\"true\" to get all results.]")

    if not lines:
        return [f"No matches found for '{pattern}' in {search_path}"]
    return lines


def cmd_glob(args: dict) -> list[str]:
    pattern = args.get("pattern", "")
    if not pattern:
        return ["Error: 'pattern' is required"]

    base = validate_path(args.get("path", str(ALLOWED_ROOTS[0])))

    try:
        matches = sorted(base.rglob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception as e:
        return [f"Error: {e}"]

    if not matches:
        return [f"No files matching '{pattern}' in {base}"]

    results = []
    for m in matches[:200]:
        try:
            rel = m.relative_to(base)
        except ValueError:
            rel = m
        results.append(str(rel))
    return results


def cmd_list_dir(args: dict) -> list[str]:
    target = validate_path(args.get("path", str(ALLOWED_ROOTS[0])))

    if not target.is_dir():
        return [f"Error: not a directory: {target}"]

    try:
        entries = list(os.scandir(target))
    except PermissionError:
        return [f"Error: permission denied: {target}"]
    except Exception as e:
        return [f"Error: {e}"]

    dirs = sorted([e for e in entries if e.is_dir()], key=lambda e: e.name.lower())
    files = sorted([e for e in entries if e.is_file()], key=lambda e: e.name.lower())

    lines = []
    for d in dirs:
        lines.append(f"📁 {d.name}/")
    for f in files:
        size = f.stat().st_size
        if size > 1_048_576:
            sz = f"{size / 1_048_576:.1f}MB"
        elif size > 1024:
            sz = f"{size / 1024:.1f}KB"
        else:
            sz = f"{size}B"
        lines.append(f"📄 {f.name} ({sz})")

    if not lines:
        return [f"(empty directory: {target})"]
    return lines


COMMANDS = {
    "grep": cmd_grep,
    "glob": cmd_glob,
    "list_dir": cmd_list_dir,
}


def main():
    raw = sys.stdin.read().strip()
    if not raw:
        print(json.dumps({"error": "No input"}))
        sys.exit(1)

    try:
        req = json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"Invalid JSON: {e}"}))
        sys.exit(1)

    command = req.get("command", "")
    if command not in COMMANDS:
        print(json.dumps({"error": f"Unknown command: {command}. Valid: {list(COMMANDS.keys())}"}))
        sys.exit(1)

    try:
        result_lines = COMMANDS[command](req.get("args", {}))
        print(json.dumps({"command": command, "lines": result_lines}))
    except PermissionError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"error": f"{command} failed: {e}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
#
