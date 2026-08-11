#!/usr/bin/env python3
"""Standalone tests for memory dedup logic.

No real embeddings, no LLM, no server required.
Uses a FakeSearcher with predetermined similarity responses.

Run: python3 tests/test_memory_dedup.py
"""

import sys, os, json, asyncio

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class FakeSearcher:
    """Simulates embedding search with configurable similarity responses.
    
    responses maps a key_substring -> list of {key, value, category, score}.
    Matches if the key_substring appears anywhere in the search query.
    """

    def __init__(self, responses: dict[str, list[dict]] | None = None):
        self._responses = responses or {}

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        # Normalize same way the real function builds entry_text: "key: value"
        # Then normalize underscores and collapse whitespace for matching
        import re
        q = re.sub(r'\s+', ' ', query.lower().replace("_", " ").replace(":", " ")).strip()
        results = []
        for substring, hits in self._responses.items():
            sub_norm = re.sub(r'\s+', ' ', substring.lower().replace("_", " ")).strip()
            if sub_norm in q:
                results.extend(hits[:top_k])
        results.sort(key=lambda r: r.get("score", 0), reverse=True)
        return results[:top_k]


def make_entry(key: str, value: str) -> dict[str, str]:
    return {"key": key, "value": value}


def run_dedup(adds: dict, searcher: FakeSearcher, existing: dict | None = None) -> tuple[dict, list]:
    """Import and call the actual _dedup_and_resolve_adds function."""
    from server.api.routes.memory import _dedup_and_resolve_adds

    if existing is None:
        existing = {}

    result = _dedup_and_resolve_adds(adds, existing, searcher)
    # Handle both old (3-tuple) and new (2-tuple) return signatures
    if len(result) == 3:
        filtered, skipped, updated = result
        review_queue = []
    else:
        filtered, review_queue = result

    return filtered, review_queue


# ─── Test Cases ───────────────────────────────────────────────

passed = 0
failed = 0


def check(name: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))


print("=" * 60)
print("Memory Dedup Logic Tests")
print("=" * 60)

# ── Test 1: Exact duplicate (>= 0.85) → queued for review ──
print("\n[Test 1] Exact duplicate (similarity >= 0.85)")
searcher = FakeSearcher({
    "favorite color": [{
        "key": "favorite_color", "value": "blue",
        "category": "semantic", "score": 0.92
    }]
})
adds = {"semantic": [make_entry("favorite_color", "blue")]}
filtered, review_queue = run_dedup(adds, searcher)
check("Entry not in filtered adds", len(filtered.get("semantic", [])) == 0)
check("Entry in review queue", len(review_queue) == 1)
check("Review score >= 0.85", review_queue[0]["score"] >= 0.85 if review_queue else False)

# ── Test 2: Near-miss / contradiction (0.70–0.85) → queued ──
print("\n[Test 2] Near-miss / contradiction (0.70 <= sim < 0.85)")
searcher = FakeSearcher({
    "python version": [{
        "key": "python_version", "value": "3.11",
        "category": "semantic", "score": 0.78
    }]
})
adds = {"semantic": [make_entry("python_version", "3.12")]}
filtered, review_queue = run_dedup(adds, searcher)
check("Entry not in filtered adds", len(filtered.get("semantic", [])) == 0)
check("Entry in review queue", len(review_queue) == 1)
check("Review score in range", 0.70 <= review_queue[0]["score"] < 0.85 if review_queue else False)

# ── Test 3: Distinct entry (< 0.70) → passes through ──
print("\n[Test 3] Distinct entry (similarity < 0.70)")
searcher = FakeSearcher({
    "docker setup": [{
        "key": "favorite_food", "value": "pizza",
        "category": "semantic", "score": 0.35
    }]
})
adds = {"semantic": [make_entry("docker_setup", "Use compose v2")]}
filtered, review_queue = run_dedup(adds, searcher)
check("Entry passes through", len(filtered.get("semantic", [])) == 1)
check("Not in review queue", len(review_queue) == 0)

# ── Test 4: Empty searcher → all pass through ──
print("\n[Test 4] Empty searcher (no indexed data)")
searcher = FakeSearcher({})
adds = {"semantic": [make_entry("new_fact", "something new")]}
filtered, review_queue = run_dedup(adds, searcher)
check("Entry passes through", len(filtered.get("semantic", [])) == 1)
check("No review queue entries", len(review_queue) == 0)

# ── Test 5: Cross-category matching ──
print("\n[Test 5] Cross-category matching")
searcher = FakeSearcher({
    "github token": [{
        "key": "gh_pat", "value": "ghp_xxx stored in config",
        "category": "episodic", "score": 0.88
    }]
})
adds = {"semantic": [make_entry("github_token_location", "PAT ghp_xxx in mcp_servers.json")]}
filtered, review_queue = run_dedup(adds, searcher)
check("Semantic entry not in filtered", len(filtered.get("semantic", [])) == 0)
check("Queued for review (cross-cat)", len(review_queue) == 1)
check("Matched against episodic", review_queue[0].get("existing_entry", {}).get("key") == "gh_pat" if review_queue else False)

# ── Test 6: Protected entries pass through unchanged ──
print("\n[Test 6] Protected entries pass through unchanged")
searcher = FakeSearcher({
    "user name": [{
        "key": "user_name", "value": "Sifat",
        "category": "protected", "score": 0.95
    }]
})
adds = {
    "protected": [make_entry("user_name", "Sifat")],
    "semantic": [make_entry("some_fact", "test")]
}
filtered, review_queue = run_dedup(adds, searcher)
check("Protected entry preserved", len(filtered.get("protected", [])) == 1)
check("Protected value unchanged", filtered["protected"][0]["value"] == "Sifat")

# ── Test 7: Ephemeral entries pass through unchanged ──
print("\n[Test 7] Ephemeral entries pass through unchanged")
searcher = FakeSearcher({})
adds = {"ephemeral": [make_entry("temp_note", "remember this for now")]}
filtered, review_queue = run_dedup(adds, searcher)
check("Ephemeral preserved", len(filtered.get("ephemeral", [])) == 1)
check("Not in review queue", len(review_queue) == 0)

# ── Test 8: Multiple new entries, some conflict ──
print("\n[Test 8] Multiple entries — mixed conflicts and passes")
searcher = FakeSearcher({
    "os preference arch": [{
        "key": "os_preference", "value": "Arch Linux",
        "category": "semantic", "score": 0.90
    }],
    "editor vscode": [{
        "key": "editor", "value": "neovim",
        "category": "semantic", "score": 0.75
    }]
})
adds = {"semantic": [
    make_entry("os_preference", "Arch Linux"),       # exact dup → review
    make_entry("editor_vscode", "vscode is my editor"),  # near-miss → review
    make_entry("keyboard_layout", "QWERTY"),          # distinct → pass
]}
filtered, review_queue = run_dedup(adds, searcher)
check("One entry passes through", len(filtered.get("semantic", [])) == 1)
check("Passed entry is keyboard_layout", filtered["semantic"][0]["key"] == "keyboard_layout")
check("Two entries in review queue", len(review_queue) == 2)

# ── Test 9: Entry with missing key → skipped ──
print("\n[Test 9] Entry with missing key")
searcher = FakeSearcher({})
adds = {"semantic": [{"value": "no key here"}, make_entry("valid_key", "valid value")]}
filtered, review_queue = run_dedup(adds, searcher)
check("Only valid entry passes", len(filtered.get("semantic", [])) == 1)
check("Valid entry has correct key", filtered["semantic"][0]["key"] == "valid_key")

# ── Test 10: Intra-batch dedup (new vs new) ──
print("\n[Test 10] Intra-batch dedup (entries similar to each other)")
searcher = FakeSearcher({})  # No existing matches
adds = {"semantic": [
    make_entry("hyprland_blur_config", "enable blur on all windows in hyprland config settings"),
    make_entry("hyprland_blur_setting", "blur enabled for all windows in hyprland config settings"),
    make_entry("unrelated_fact", "python uses gil for threading"),
]}
filtered, review_queue = run_dedup(adds, searcher)
# Two hyprland entries should trigger intra-batch dedup
kept_keys = [e["key"] for e in filtered.get("semantic", [])]
check("Unrelated fact passes through", "unrelated_fact" in kept_keys)
check("At least one hyprland entry kept", any("hyprland" in k for k in kept_keys))
check("Intra-batch conflict detected", len(review_queue) >= 1,
      f"review_queue has {len(review_queue)} entries")

# ── Summary ──────────────────────────────────────────────────
print("\n" + "=" * 60)
total = passed + failed
print(f"Results: {passed}/{total} passed" + (f", {failed} FAILED" if failed else " 🎉"))
print("=" * 60)
sys.exit(0 if failed == 0 else 1)
