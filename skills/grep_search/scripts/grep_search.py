
#!/usr/bin/env python3
"""Grep Search — ripgrep/glob/list_dir with path sandboxing."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ALLOWED_ROOTS = [
    Path(os.environ.get("PROJECT_ROOT", "/home/sifat/hdd/projects/Sable")).resolve(),
    Path.home().resolve(),
    Path("/tmp").resolve(),
]

MAX_RESULTS_CAP = 200


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

    # Try ripgrep first
    rg = shutil.which("rg")
    if rg:
        cmd = [rg, "--json", "--max-count=1", f"--max-count={max_results}"]
        if ignore_case:
            cmd.append("-i")
        if glob_filter:
            cmd.extend(["--glob", glob_filter])
        cmd.extend([pattern, str(search_path)])
    else:
        # Fallback to grep -rn
        cmd = ["grep", "-rn", "--max-count=1"]
        if ignore_case:
            cmd.append("-i")
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
    if rg:
        # Parse ripgrep JSON lines
        for line in proc.stdout.splitlines():
            if len(lines) >= max_results:
                break
            try:
                obj = json.loads(line)
                if obj.get("type") == "match":
                    data = obj["data"]
                    path = data["path"]["text"]
                    line_num = data["line_number"]
                    text = data["lines"]["text"].rstrip("\n")
                    lines.append(f"{path}:{line_num}:{text}")
            except (json.JSONDecodeError, KeyError):
                continue
    else:
        # grep output is already file:line:match
        for line in proc.stdout.splitlines():
            if len(lines) >= max_results:
                break
            lines.append(line)

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
