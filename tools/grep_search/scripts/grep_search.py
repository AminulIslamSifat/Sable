
#!/usr/bin/env python3
"""Grep Search — ripgrep/glob/list_dir with path sandboxing."""

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
    rg = shutil.which("rg")
    if rg:
        cmd = [rg, "--json", "--max-count=1", f"--max-count={max_results}"]
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
    else:
        # Fallback to grep -rn
        cmd = ["grep", "-rn", "--max-count=1"]
        if ignore_case:
            cmd.append("-i")
        default_excl_dirs = ["vendor", "node_modules"]
        default_excl_files = ["*.min.js", "*.min.css"]
        for excl in extra_excludes:
            if excl.endswith("/"):
                default_excl_dirs.append(excl.rstrip("/"))
            elif "*" in excl or "." in excl:
                default_excl_files.append(excl)
            else:
                default_excl_dirs.append(excl)
        for d in default_excl_dirs:
            cmd.append(f"--exclude-dir={d}")
        for f in default_excl_files:
            cmd.append(f"--exclude={f}")
        if glob_filter:
            cmd.extend(["--include", glob_filter])
        cmd.extend([pattern, str(search_path)])

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return ["Error: search timed out after 30s"]
    except Exception as e:
        return [f"Error: {e}"]

    lines: list[str] = []
    total_chars = 0
    truncated = False
    total_match_count = 0

    if rg:
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
    else:
        # grep output is already file:line:match
        for line in proc.stdout.splitlines():
            if len(lines) >= max_results:
                break
            total_match_count += 1
            if len(line) > LINE_TRUNCATE:
                line = line[:LINE_TRUNCATE] + "…"
            if not full_output and total_chars + len(line) > MAX_OUTPUT_CHARS:
                truncated = True
                break
            total_chars += len(line)
            lines.append(line)

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
