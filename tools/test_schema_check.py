"""Ad-hoc check: does SchemaValidationMiddleware actually reject bad calls?

Not a pytest suite — a readable table so you can eyeball the behavior.
"""

import json
import sys

sys.path.insert(0, "/home/sifat/hdd/projects/test/Sable-exp")

from engine.skills.schema_check import validate_arguments
from engine.tools_loader import load_single_tool


CASES = [
    # (tool, args, expectation)  expectation: True = should pass
    ("memory", {"action": "add"}, False),
    ("memory", {"action": "add", "key": "k", "value": "v"}, True),
    ("memory", {"action": "merge", "target_key": "t"}, False),
    ("memory", {"action": "merge", "target_key": "t", "source_keys": ["a"]}, True),
    ("memory", {"action": "search", "query": "q"}, True),
    ("memory", {"action": "search"}, False),
    ("memory", {"action": "bogus"}, False),
    ("computer_use", {"action": "mouse_click", "x": "10"}, False),
    ("computer_use", {"action": "mouse_click", "x": "10", "y": "20"}, True),
    ("computer_use", {"action": "keyboard_type"}, False),
    ("computer_use", {"action": "keyboard_type", "text": "hi"}, True),
    ("execute_command", {"command": "ls", "timeout": "999"}, False),
    ("execute_command", {"command": "ls", "timeout": "30"}, True),
    ("execute_command", {"timeout": "30"}, False),
    ("ask_user", {"question": "q", "options": ["a"]}, False),
    ("ask_user", {"question": "q", "options": ["a", "b"]}, True),
    ("tracknote", {"action": "toggle_item", "note_id": "n"}, False),
    ("tracknote", {"action": "toggle_item", "note_id": "n", "index": "2"}, True),
    ("tracknote", {"action": "delete", "id": "x"}, False),
    ("tracknote", {"action": "delete", "kind": "notes", "id": "x"}, True),
]


def schema_for(tool: str):
    loaded = load_single_tool(tool)
    if not loaded:
        return None
    return (loaded[0].get("function") or {}).get("parameters")


fails = 0
for tool, args, expect in CASES:
    schema = schema_for(tool)
    if schema is None:
        print(f"SKIP   {tool:15} (no schema found)")
        continue
    ok, errs = validate_arguments(args, schema)
    verdict = "ok " if ok == expect else "BAD"
    if ok != expect:
        fails += 1
    detail = "" if ok else "  -> " + "; ".join(errs)
    print(f"{verdict}  {'PASS' if ok else 'REJECT'}  {tool:15} {json.dumps(args)}{detail}")

print()
print("ALL EXPECTATIONS MET" if fails == 0 else f"{fails} expectation(s) NOT met")
