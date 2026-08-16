#!/usr/bin/env python3
"""Memory Manager CLI — CRUD + merge + search for Brain memory files.

Usage:
    python3 memory_manager.py <action> [options]

Actions: list, get, add, update, delete, merge, search
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# ─── Paths ───────────────────────────────────────────────────────────────────
_BRAIN_DIR = Path(__file__).resolve().parent.parent.parent / "Brain"
_MEMORY_PATH = _BRAIN_DIR / "Memory.json"
_PROCEDURAL_PATH = _BRAIN_DIR / "Procedural.json"
_PROTECTED_PATH = _BRAIN_DIR / "Protected.json"

CATEGORIES_MEMORY = ("semantic", "episodic", "ephemeral")
CATEGORIES_ALL = (*CATEGORIES_MEMORY, "procedural", "protected")


def _load_json(path: Path) -> dict | list:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, (dict, list)) else {}
    except Exception:
        return {}


def _save_json(path: Path, data: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _reload_searcher() -> None:
    try:
        from engine.memory_search import get_searcher
        get_searcher().reload_memory()
    except Exception:
        pass


def _get_protected_keys() -> set[str]:
    keys: set[str] = set()
    data = _load_json(_PROTECTED_PATH)
    entries = data.get("protected", []) if isinstance(data, dict) else []
    for e in entries:
        if isinstance(e, dict) and e.get("key"):
            keys.add(e["key"])
    return keys


def _find_entry(category: str, key: str) -> tuple[dict | None, str]:
    """Find an entry by category and key. Returns (entry_dict, file_source)."""
    if category == "procedural":
        data = _load_json(_PROCEDURAL_PATH)
        entries = data.get("procedural", []) if isinstance(data, dict) else []
        for e in entries:
            if isinstance(e, dict) and e.get("key") == key:
                return e, "procedural"
        return None, "procedural"

    if category == "protected":
        data = _load_json(_PROTECTED_PATH)
        entries = data.get("protected", []) if isinstance(data, dict) else []
        for e in entries:
            if isinstance(e, dict) and e.get("key") == key:
                return e, "protected"
        return None, "protected"

    # Memory.json categories
    data = _load_json(_MEMORY_PATH)
    if not isinstance(data, dict):
        return None, "memory"
    entries = data.get(category, [])
    for e in entries:
        if isinstance(e, dict) and e.get("key") == key:
            return e, "memory"
    return None, "memory"


# ─── Actions ─────────────────────────────────────────────────────────────────

def action_list(args) -> None:
    cat = args.category
    results: dict[str, list] = {}

    if cat and cat != "all":
        if cat not in CATEGORIES_ALL:
            print(f"ERROR: Unknown category '{cat}'. Valid: {', '.join(CATEGORIES_ALL)}, all", file=sys.stderr)
            sys.exit(1)
        cats_to_load = [cat]
    else:
        cats_to_load = list(CATEGORIES_ALL)

    for c in cats_to_load:
        if c == "procedural":
            data = _load_json(_PROCEDURAL_PATH)
            entries = data.get("procedural", []) if isinstance(data, dict) else []
        elif c == "protected":
            data = _load_json(_PROTECTED_PATH)
            entries = data.get("protected", []) if isinstance(data, dict) else []
        else:
            data = _load_json(_MEMORY_PATH)
            entries = data.get(c, []) if isinstance(data, dict) else []
        results[c] = entries

    # Compact output: key + truncated value
    total = sum(len(v) for v in results.values())
    print(f"Memory entries: {total} total")
    for c, entries in results.items():
        if not entries:
            continue
        print(f"\n## {c} ({len(entries)} entries)")
        for e in entries:
            if not isinstance(e, dict):
                continue
            k = e.get("key", "???")
            v = e.get("value", "")
            preview = v[:120].replace("\n", " ") + ("…" if len(v) > 120 else "")
            print(f"  • {k}")
            if preview:
                print(f"    {preview}")


def action_get(args) -> None:
    if not args.key:
        print("ERROR: --key is required for 'get'", file=sys.stderr)
        sys.exit(1)

    cat = args.category
    if cat:
        entry, source = _find_entry(cat, args.key)
        if entry is None:
            print(f"Not found: key='{args.key}' in category='{cat}'")
            sys.exit(1)
        print(json.dumps(entry, indent=2, ensure_ascii=False))
    else:
        # Search all categories
        for c in CATEGORIES_ALL:
            entry, source = _find_entry(c, args.key)
            if entry is not None:
                print(f"Found in: {source}/{c}")
                print(json.dumps(entry, indent=2, ensure_ascii=False))
                return
        print(f"Not found: key='{args.key}'")
        sys.exit(1)


def action_add(args) -> None:
    if not args.key or not args.value:
        print("ERROR: --key and --value are required for 'add'", file=sys.stderr)
        sys.exit(1)

    cat = args.category or "semantic"
    if cat not in CATEGORIES_ALL:
        print(f"ERROR: Unknown category '{cat}'", file=sys.stderr)
        sys.exit(1)

    # Check for duplicate
    existing, _ = _find_entry(cat, args.key)
    if existing is not None:
        print(f"ERROR: Key '{args.key}' already exists in '{cat}'. Use 'update' instead.", file=sys.stderr)
        sys.exit(1)

    entry: dict = {"key": args.key, "value": args.value}
    if args.tags:
        entry["tags"] = args.tags
    if args.triggers:
        entry["triggers"] = args.triggers
    if cat == "procedural":
        if args.trigger:
            entry["trigger"] = args.trigger
        if args.keywords:
            entry["keywords"] = args.keywords

    if cat == "procedural":
        data = _load_json(_PROCEDURAL_PATH)
        if not isinstance(data, dict):
            data = {}
        proc_list = data.get("procedural", [])
        proc_list.append(entry)
        data["procedural"] = proc_list
        _save_json(_PROCEDURAL_PATH, data)
    elif cat == "protected":
        data = _load_json(_PROTECTED_PATH)
        if not isinstance(data, dict):
            data = {}
        prot_list = data.get("protected", [])
        prot_list.append(entry)
        data["protected"] = prot_list
        _save_json(_PROTECTED_PATH, data)
    else:
        data = _load_json(_MEMORY_PATH)
        if not isinstance(data, dict):
            data = {}
        cat_list = data.get(cat, [])
        cat_list.append(entry)
        data[cat] = cat_list
        _save_json(_MEMORY_PATH, data)

    _reload_searcher()
    print(f"Added '{args.key}' to {cat}")


def action_update(args) -> None:
    if not args.key:
        print("ERROR: --key is required for 'update'", file=sys.stderr)
        sys.exit(1)

    cat = args.category
    if not cat:
        # Auto-detect category
        for c in CATEGORIES_ALL:
            entry, source = _find_entry(c, args.key)
            if entry is not None:
                cat = c
                break
        if not cat:
            print(f"ERROR: Key '{args.key}' not found in any category", file=sys.stderr)
            sys.exit(1)

    entry, source = _find_entry(cat, args.key)
    if entry is None:
        print(f"ERROR: Key '{args.key}' not found in '{cat}'", file=sys.stderr)
        sys.exit(1)

    # Update fields
    if args.value is not None:
        entry["value"] = args.value
    if args.tags is not None:
        entry["tags"] = args.tags
    if args.triggers is not None:
        entry["triggers"] = args.triggers
    if cat == "procedural":
        if args.trigger is not None:
            entry["trigger"] = args.trigger
        if args.keywords is not None:
            entry["keywords"] = args.keywords

    # Write back
    if cat == "procedural":
        data = _load_json(_PROCEDURAL_PATH)
        if isinstance(data, dict):
            data["procedural"] = [
                entry if e.get("key") == args.key else e
                for e in data.get("procedural", [])
            ]
        _save_json(_PROCEDURAL_PATH, data)
    elif cat == "protected":
        data = _load_json(_PROTECTED_PATH)
        if isinstance(data, dict):
            data["protected"] = [
                entry if e.get("key") == args.key else e
                for e in data.get("protected", [])
            ]
        _save_json(_PROTECTED_PATH, data)
    else:
        data = _load_json(_MEMORY_PATH)
        if isinstance(data, dict):
            data[cat] = [
                entry if e.get("key") == args.key else e
                for e in data.get(cat, [])
            ]
        _save_json(_MEMORY_PATH, data)

    _reload_searcher()
    print(f"Updated '{args.key}' in {cat}")


def action_delete(args) -> None:
    if not args.key:
        print("ERROR: --key is required for 'delete'", file=sys.stderr)
        sys.exit(1)

    cat = args.category
    protected_keys = _get_protected_keys()

    if not cat:
        # Auto-detect
        for c in CATEGORIES_ALL:
            entry, source = _find_entry(c, args.key)
            if entry is not None:
                cat = c
                break
        if not cat:
            print(f"ERROR: Key '{args.key}' not found in any category", file=sys.stderr)
            sys.exit(1)

    # Safety: never delete protected keys unless explicitly targeting protected category
    if args.key in protected_keys and cat != "protected":
        print(f"ERROR: Key '{args.key}' is protected. Cannot delete from '{cat}'.", file=sys.stderr)
        sys.exit(1)

    entry, source = _find_entry(cat, args.key)
    if entry is None:
        print(f"ERROR: Key '{args.key}' not found in '{cat}'", file=sys.stderr)
        sys.exit(1)

    if cat == "procedural":
        data = _load_json(_PROCEDURAL_PATH)
        if isinstance(data, dict):
            data["procedural"] = [e for e in data.get("procedural", []) if e.get("key") != args.key]
        _save_json(_PROCEDURAL_PATH, data)
    elif cat == "protected":
        data = _load_json(_PROTECTED_PATH)
        if isinstance(data, dict):
            data["protected"] = [e for e in data.get("protected", []) if e.get("key") != args.key]
        _save_json(_PROTECTED_PATH, data)
    else:
        data = _load_json(_MEMORY_PATH)
        if isinstance(data, dict):
            data[cat] = [e for e in data.get(cat, []) if e.get("key") != args.key]
        _save_json(_MEMORY_PATH, data)

    _reload_searcher()
    print(f"Deleted '{args.key}' from {cat}")


def action_merge(args) -> None:
    """Merge source_keys into target_key. Concatenates values, unions tags/triggers."""
    if not args.target_key or not args.source_keys:
        print("ERROR: --target-key and --source-keys are required for 'merge'", file=sys.stderr)
        sys.exit(1)

    cat = args.category
    if not cat:
        # Auto-detect from target
        for c in CATEGORIES_ALL:
            entry, _ = _find_entry(c, args.target_key)
            if entry is not None:
                cat = c
                break
        if not cat:
            print(f"ERROR: Target key '{args.target_key}' not found", file=sys.stderr)
            sys.exit(1)

    target_entry, _ = _find_entry(cat, args.target_key)
    if target_entry is None:
        print(f"ERROR: Target key '{args.target_key}' not found in '{cat}'", file=sys.stderr)
        sys.exit(1)

    merged_value_parts = [target_entry.get("value", "")]
    merged_tags = set(target_entry.get("tags", []))
    merged_triggers = set(target_entry.get("triggers", []))
    merged_keywords = set(target_entry.get("keywords", []))
    deleted_sources = []

    for src_key in args.source_keys:
        src_entry, src_source = _find_entry(cat, src_key)
        if src_entry is None:
            print(f"WARNING: Source key '{src_key}' not found in '{cat}', skipping")
            continue
        merged_value_parts.append(src_entry.get("value", ""))
        merged_tags.update(src_entry.get("tags", []))
        merged_triggers.update(src_entry.get("triggers", []))
        merged_keywords.update(src_entry.get("keywords", []))
        deleted_sources.append(src_key)

    # Update target
    target_entry["value"] = "\n\n".join(p for p in merged_value_parts if p)
    if merged_tags:
        target_entry["tags"] = sorted(merged_tags)
    if merged_triggers:
        target_entry["triggers"] = sorted(merged_triggers)
    if merged_keywords and cat == "procedural":
        target_entry["keywords"] = sorted(merged_keywords)

    # Write updated target
    if cat == "procedural":
        data = _load_json(_PROCEDURAL_PATH)
        if isinstance(data, dict):
            data["procedural"] = [
                target_entry if e.get("key") == args.target_key else e
                for e in data.get("procedural", [])
            ]
            # Remove sources
            data["procedural"] = [e for e in data["procedural"] if e.get("key") not in deleted_sources]
        _save_json(_PROCEDURAL_PATH, data)
    elif cat == "protected":
        data = _load_json(_PROTECTED_PATH)
        if isinstance(data, dict):
            data["protected"] = [
                target_entry if e.get("key") == args.target_key else e
                for e in data.get("protected", [])
            ]
            data["protected"] = [e for e in data["protected"] if e.get("key") not in deleted_sources]
        _save_json(_PROTECTED_PATH, data)
    else:
        data = _load_json(_MEMORY_PATH)
        if isinstance(data, dict):
            data[cat] = [
                target_entry if e.get("key") == args.target_key else e
                for e in data.get(cat, [])
            ]
            data[cat] = [e for e in data[cat] if e.get("key") not in deleted_sources]
        _save_json(_MEMORY_PATH, data)

    _reload_searcher()
    print(f"Merged {len(deleted_sources)} source(s) into '{args.target_key}' in {cat}")
    if deleted_sources:
        print(f"  Deleted sources: {', '.join(deleted_sources)}")


def action_search(args) -> None:
    if not args.query:
        print("ERROR: --query is required for 'search'", file=sys.stderr)
        sys.exit(1)

    try:
        from engine.memory_search import get_searcher
        searcher = get_searcher()
        results = searcher.search(args.query, top_k=args.top_k)
        if not results:
            print(f"No results for: {args.query}")
            return
        print(f"Search results for: {args.query} ({len(results)} hits)\n")
        for i, r in enumerate(results, 1):
            score = r.get("score", 0)
            cat = r.get("category", "?")
            key = r.get("key", "???")
            val = r.get("value", "")[:200].replace("\n", " ")
            print(f"{i}. [{cat}] {key} (score: {score:.4f})")
            print(f"   {val}")
    except ImportError:
        # Fallback: simple substring search
        query_lower = args.query.lower()
        hits = []
        for c in CATEGORIES_ALL:
            if c == "procedural":
                data = _load_json(_PROCEDURAL_PATH)
                entries = data.get("procedural", []) if isinstance(data, dict) else []
            elif c == "protected":
                data = _load_json(_PROTECTED_PATH)
                entries = data.get("protected", []) if isinstance(data, dict) else []
            else:
                data = _load_json(_MEMORY_PATH)
                entries = data.get(c, []) if isinstance(data, dict) else []
            for e in entries:
                if not isinstance(e, dict):
                    continue
                text = f"{e.get('key', '')} {e.get('value', '')}".lower()
                if query_lower in text:
                    hits.append((c, e))

        if not hits:
            print(f"No results for: {args.query}")
            return
        print(f"Substring search: {args.query} ({len(hits)} hits)\n")
        for i, (c, e) in enumerate(hits[:20], 1):
            key = e.get("key", "???")
            val = e.get("value", "")[:200].replace("\n", " ")
            print(f"{i}. [{c}] {key}")
            print(f"   {val}")


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Sable Memory Manager")
    parser.add_argument("action", choices=["list", "get", "add", "update", "delete", "merge", "search"])
    parser.add_argument("--category", "-c", help="Memory category")
    parser.add_argument("--key", "-k", help="Entry key")
    parser.add_argument("--value", "-v", help="Entry value")
    parser.add_argument("--tags", nargs="*", help="Tags")
    parser.add_argument("--triggers", nargs="*", help="Trigger phrases")
    parser.add_argument("--keywords", nargs="*", help="Keywords (procedural)")
    parser.add_argument("--trigger", help="Trigger string (procedural)")
    parser.add_argument("--query", "-q", help="Search query")
    parser.add_argument("--target-key", help="Target key for merge")
    parser.add_argument("--source-keys", nargs="*", help="Source keys for merge")
    parser.add_argument("--top-k", type=int, default=10, help="Max search results")

    args = parser.parse_args()

    actions = {
        "list": action_list,
        "get": action_get,
        "add": action_add,
        "update": action_update,
        "delete": action_delete,
        "merge": action_merge,
        "search": action_search,
    }
    actions[args.action](args)


if __name__ == "__main__":
    main()
#
