#!/usr/bin/env python3
"""tools/validate.py — cross-check tool.json schemas against HANDLER_MAP.

Catches the schema/handler drift class that nothing else does:

  1. schema name with no handler  -> model calls it, runtime "No handler registered"
  2. handler with no schema       -> dead code, or unadvertised capability
  3. duplicate function names across folders
  4. required[] keys not present in properties{}
  5. missing manifest.json        -> tool would silently pick a default tier

Exit 0 = clean, 1 = errors found. Intended for CI and pre-commit.

Usage:
    python3 tools/validate.py
    python3 tools/validate.py --quiet
    python3 tools/validate.py --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO / "tools"
HANDLERS_INIT = REPO / "engine" / "skills" / "handlers" / "__init__.py"

# Handlers that are intentionally reachable without their own tool.json.
# Every entry needs a one-line reason — if you can't justify it, add a schema.
_ALIASES: dict[str, str] = {
    "read_file": "alias of get_file",
    "openweb": "alias of online_search",
    "online_search": "alias of web_search (web.py)",
    "memory_manager": "alias of memory",
}

# Names synthesised at prompt-build time rather than read from disk.
_SYNTHETIC: dict[str, str] = {
    "load_tool": "built by tools_loader._build_load_tool_schema",
    "chat_title": "intercepted by server/api/routes/chat.py regex, never hits HANDLER_MAP",
}

# Tier values the loader accepts.
_TIERS = {"core", "outer"}


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.info: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def note(self, msg: str) -> None:
        self.info.append(msg)


# ─── Handlers ────────────────────────────────────────────────────────────────

def load_handler_names() -> set[str]:
    """Import HANDLER_MAP. Falls back to regex-parsing the source if the
    import fails (e.g. running outside the venv)."""
    sys.path.insert(0, str(REPO))
    try:
        from engine.skills.handlers import HANDLER_MAP  # type: ignore
        return set(HANDLER_MAP.keys())
    except Exception as exc:  # pragma: no cover - defensive fallback
        print(f"  (import failed: {exc}; falling back to source parse)", file=sys.stderr)
        if not HANDLERS_INIT.exists():
            return set()
        src = HANDLERS_INIT.read_text(encoding="utf-8")
        block = re.search(r"HANDLER_MAP[^=]*=\s*\{(.*?)\}", src, re.DOTALL)
        if not block:
            return set()
        return set(re.findall(r'"([^"]+)"\s*:', block.group(1)))


# ─── Schemas ─────────────────────────────────────────────────────────────────

def load_schemas(report: Report) -> dict[str, list[str]]:
    """Return {function_name: [source tool.json paths]}.

    A list is used so duplicate names across folders are visible.
    """
    schemas: dict[str, list[str]] = {}
    for manifest in sorted(TOOLS_DIR.glob("*/tool.json")):
        rel = manifest.relative_to(REPO)
        try:
            raw = json.loads(manifest.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            report.error(f"{rel}: unparseable ({exc})")
            continue
        if not isinstance(raw, list):
            report.error(f"{rel}: top level must be a JSON array")
            continue
        for fn in raw:
            if not isinstance(fn, dict) or not fn.get("name"):
                report.error(f"{rel}: entry missing 'name'")
                continue
            name = fn["name"]
            schemas.setdefault(name, []).append(str(rel))
            _check_required(name, fn, rel, report)
    return schemas


def _check_required(name: str, fn: dict, rel: Path, report: Report) -> None:
    """Every key in required[] must exist in properties{}."""
    params = fn.get("parameters") or {}
    if not isinstance(params, dict):
        return
    props = params.get("properties") or {}
    required = params.get("required") or []
    for key in required:
        if key not in props:
            report.error(f"{rel}: '{name}' requires '{key}' but it is not in properties")


# ─── Manifests ───────────────────────────────────────────────────────────────

def check_manifests(report: Report) -> None:
    for tool_dir in sorted(p for p in TOOLS_DIR.iterdir() if p.is_dir()):
        if tool_dir.name.startswith("."):
            continue
        if not (tool_dir / "tool.json").exists():
            continue
        manifest = tool_dir / "manifest.json"
        if not manifest.exists():
            report.error(
                f"tools/{tool_dir.name}/: missing manifest.json "
                f'(add {{"tier": "core"}} or {{"tier": "outer"}})'
            )
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            report.error(f"tools/{tool_dir.name}/manifest.json: invalid ({exc})")
            continue
        tier = str(data.get("tier", "")).lower()
        if tier not in _TIERS:
            report.error(
                f'tools/{tool_dir.name}/manifest.json: tier must be "core" or "outer" (got {tier!r})'
            )


# ─── Cross-checks ────────────────────────────────────────────────────────────

def cross_check(schemas: dict[str, list[str]], handlers: set[str], report: Report) -> None:
    schema_names = set(schemas)

    # 1. schema with no handler (synthetic names are dispatched elsewhere)
    for name in sorted(schema_names):
        if name in _SYNTHETIC:
            continue
        if name not in handlers:
            report.error(
                f"schema '{name}' ({schemas[name][0]}) has NO handler in HANDLER_MAP "
                f"-> model can call it, runtime will fail"
            )

    # 2. handler with no schema
    for name in sorted(handlers):
        if name in schema_names or name in _ALIASES or name in _SYNTHETIC:
            continue
        report.error(
            f"handler '{name}' has NO tool.json and is not in _ALIASES "
            f"-> dead code or missing advertisement"
        )

    # 3. duplicates
    for name, sources in sorted(schemas.items()):
        if len(sources) > 1:
            report.error(f"duplicate function '{name}' in: {', '.join(sources)}")

    # 4. stale alias entries
    for alias, reason in sorted(_ALIASES.items()):
        if alias in schema_names:
            report.warn(f"alias '{alias}' now has a tool.json — remove from _ALIASES ({reason})")
        if alias not in handlers:
            report.warn(f"alias '{alias}' is not in HANDLER_MAP — remove from _ALIASES ({reason})")

    report.note(f"{len(schema_names)} schema functions, {len(handlers)} handlers")


# ─── Main ────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Validate Sable tool schemas vs handlers")
    ap.add_argument("--quiet", action="store_true", help="only print errors")
    ap.add_argument("--json", action="store_true", help="emit machine-readable report")
    args = ap.parse_args()

    report = Report()
    handlers = load_handler_names()
    if not handlers:
        report.error("could not load HANDLER_MAP — is the venv active?")
    schemas = load_schemas(report)
    check_manifests(report)
    cross_check(schemas, handlers, report)

    if args.json:
        print(json.dumps({
            "errors": report.errors,
            "warnings": report.warnings,
            "info": report.info,
        }, indent=2))
        return 1 if report.errors else 0

    if not args.quiet:
        for line in report.info:
            print(f"  info: {line}")
        for line in report.warnings:
            print(f"  warn: {line}")

    if report.errors:
        print(f"\n{len(report.errors)} error(s):")
        for line in report.errors:
            print(f"  ERROR: {line}")
        return 1

    print("\nOK — schemas and handlers agree.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
