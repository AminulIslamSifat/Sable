"""End-to-end check: does a bad tag die at SchemaValidationMiddleware
before reaching the handler?

This exercises the REAL pipeline built by SkillEngine, not the validator alone.
"""

import sys

sys.path.insert(0, "/home/sifat/hdd/projects/test/Sable-exp")

from pathlib import Path

from engine.skills.engine import SkillEngine
from engine.skills.handlers import HANDLER_MAP

engine = SkillEngine(
    skills_dir=Path("/home/sifat/hdd/projects/test/Sable-exp/skills"),
    handlers=HANDLER_MAP,
    agent_id="maria",
)


CASES = [
    # (label, tag, attrs, content)
    ("bad memory.add (no value)", "memory", {"action": "add", "key": "k"}, ""),
    ("bad memory.merge (no sources)", "memory", {"action": "merge", "target_key": "t"}, ""),
    ("bad computer_use mouse_click", "computer_use", {"action": "mouse_click", "x": "5"}, ""),
    ("bad tracknote delete", "tracknote", {"action": "delete", "id": "x"}, ""),
]

for label, tag, attrs, content in CASES:
    events = list(engine.process_tag(tag, attrs, content, chat_id="smoke"))
    ends = [e for e in events if e.get("type") == "skill_end"]
    print(f"\n--- {label} ---")
    for e in ends:
        err = e.get("error", "")
        print(f"  ok={e.get('ok')}  error={err}")
    # Show any schema rejection text
    for e in events:
        if e.get("type") == "skill_output":
            print(f"  out: {e.get('text','').strip()[:120]}")
        if e.get("type") == "skill_start":
            print(f"  (reached handler: {e.get('name')})")
