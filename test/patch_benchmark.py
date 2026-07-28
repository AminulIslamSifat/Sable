
from pathlib import Path

path = Path(file).resolve().parent / "test_embedding.py"
lines = path.read_text().split("\n")

new_lines = []
in_benchmark = False

for line in lines:
if "def benchmark_model(model_name: str) -> dict | None:" in line:
in_benchmark = True
new_lines.append(line)
new_lines.append(" detail: list[str] = []")
new_lines.append("")
new_lines.append(' def emit(s: str = "") -> None:')
new_lines.append(' print(s)')
new_lines.append(" detail.append(s)")
new_lines.append("")
continue

if in_benchmark:
if line.strip() and not line.startswith(" ") and not line.startswith("\t"):
in_benchmark = False
new_lines.append(line)
continue

stripped = line.lstrip()
if stripped.startswith("print("):
indent = line[: len(line) - len(stripped)]
rest = stripped[6:]
new_lines.append(f"{indent}emit({rest}")
else:
new_lines.append(line)
else:
new_lines.append(line)

result = "\n".join(new_lines)
count_emit = result.count("emit(")
print(f"emit() calls added: {count_emit}")
path.write_text(result)
print("Done")
