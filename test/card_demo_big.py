
"""A bigger demo file to test the tool activity card during long generation."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class CacheEntry:
    key: str
    value: Any
    created_at: float = field(default_factory=time.time)
    ttl: float = 3600.0

    @property
    def expired(self) -> bool:
        return (time.time() - self.created_at) > self.ttl

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "created_at": self.created_at,
            "ttl": self.ttl,
        }


class DiskCache:
    """Simple file-backed cache with TTL support."""

    def __init__(self, root: str | Path, default_ttl: float = 3600.0) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.default_ttl = default_ttl
        self._index: dict[str, CacheEntry] = {}

    def _path_for(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode()).hexdigest()[:16]
        return self.root / f"{digest}.json"

    def get(self, key: str) -> Any | None:
        entry = self._index.get(key)
        if entry is None:
            return None
        if entry.expired:
            self.delete(key)
            return None
        return entry.value

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        entry = CacheEntry(key=key, value=value, ttl=ttl or self.default_ttl)
        self._index[key] = entry
        path = self._path_for(key)
        path.write_text(json.dumps(entry.to_dict(), indent=2), encoding="utf-8")

    def delete(self, key: str) -> bool:
        self._index.pop(key, None)
        path = self._path_for(key)
        if path.exists():
            path.unlink()
            return True
        return False

    def clear(self) -> int:
        count = 0
        for path in self.root.glob("*.json"):
            path.unlink()
            count += 1
        self._index.clear()
        return count

    def stats(self) -> dict[str, int]:
        total = len(self._index)
        expired = sum(1 for e in self._index.values() if e.expired)
        return {"total": total, "active": total - expired, "expired": expired}


if __name__ == "__main__":
    cache = DiskCache("/tmp/demo_cache", default_ttl=60)
    cache.set("greeting", "Hello, Sifat!")
    cache.set("count", 42)
    print(cache.get("greeting"))
    print(cache.stats())
    print(f"Cleared {cache.clear()} entries")
