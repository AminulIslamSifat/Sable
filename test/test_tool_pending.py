
"""Verify SkillParser emits tool_pending events while buffering tag content."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.skills import SkillParser

CLOSE = "<" + "/create_file>"

p = SkillParser()
events = []
events += list(p.feed('Here is the file:\n<create_file path="/tmp/test.py">\ndef hello():'))
events += list(p.feed('\n    print("hi")'))
events += list(p.feed('\n' + CLOSE))
events += list(p.flush())

types = [e.get("type") for e in events]
print("Event sequence:", types)

assert "tool_pending" in types, "Missing tool_pending event!"
pending = next(e for e in events if e["type"] == "tool_pending")
assert pending["tag"] == "create_file", f"Wrong tag: {pending['tag']}"
assert pending["attrs"].get("path") == "/tmp/test.py", f"Wrong path: {pending['attrs']}"

# tool_pending must come BEFORE skill_start
pi = types.index("tool_pending")
si = types.index("skill_start")
assert pi < si, f"tool_pending ({pi}) should come before skill_start ({si})"

print("All assertions passed ✓")
