
"""Little file-stats utility — built to give the new tool-card progress
counter something real to count. Run it on any file to see lines & bytes.

Meta, right? It measures exactly what the activity card now shows you.
"""

from __future__ import annotations

import sys
from pathlib import Path


def file_stats(path: Path) -> dict[str, int | str]:
    """Return line count, byte size, and a human-readable size for a file."""
    if not path.exists():
        return {"error": f"no such file: {path}"}

    raw = path.read_bytes()
    lines = raw.count(b"\n") + (1 if raw and not raw.endswith(b"\n") else 0)
    size = len(raw)

    return {
        "path": str(path),
        "lines": lines,
        "bytes": size,
        "human": _human_size(size),
    }


def _human_size(n: int) -> str:
    """Format a byte count into a readable string (B / KB / MB)."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: progress_demo.py <file>")
        return

    stats = file_stats(Path(sys.argv[1]))
    if "error" in stats:
        print(stats["error"])
        return

    print(f"{stats['path']}")
    print(f"  {stats['lines']} lines · {stats['human']}")


if __name__ == "__main__":
    main()
