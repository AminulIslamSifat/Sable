
#!/usr/bin/env python3
"""Patch benchmark_model() to collect terminal output for file saving."""
from pathlib import Path

path = Path(file).resolve().parent / "test_embedding.py"
lines = path.read_text().split("\n")

new_lines = []
in_benchmark = False
added_emit = False

for i, line in enumerate(lines):

Detect function start

if "def benchmark_model(model_name: str) -> dict | None:" in line:
in_benchmark = True
new_lines.append(line)

Insert detail list + emit helper right after function def

new_lines.append(" detail: list[str] = [] # collect all terminal output for file saving")
new_lines.append("")
new_lines.append(' def emit(s: str = "") -> None:')
new_lines.append(' """Print to terminal AND buffer for file output."""')
new_lines.append(" print(s)")
new_lines.append(" detail.append(s)")
new_lines.append("")
continue

if in_benchmark:

Detect function end: a line with 0-indent that's not empty/comment

if line.strip() and not line.startswith(" ") and not line.startswith("\t"):
in_benchmark = False
new_lines.append(line)
continue

Replace print( with emit(

stripped = line.lstrip()
if stripped.startswith("print("):
indent = line[: len(line) - len(stripped)]
rest = stripped[6:] # remove "print("
new_lines.append(f"{indent}emit({rest}")
else:
new_lines.append(line)
else:
new_lines.append(line)

Check we actually modified something

result = "\n".join(new_lines)
count_emit = result.count("emit(")
count_print_in_func = 0

Quick sanity: no print() left inside benchmark_model

print(f"emit() calls added: {count_emit}")
path.write_text(result)
print("Done. Wrote patched test_embedding.py")
