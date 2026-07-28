
"""Quick diagnostic: test recent user prompts against memory search to see what scores they get."""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.memory_search import MemorySearcher

DB_PATH = Path(__file__).resolve().parent.parent / "sable.db"
MODEL = "jinaai/jina-embeddings-v2-small-en"
THRESHOLD = 0.596

RECENT_PROMPTS = [
    "is it not saving thinking and skill event now?",
    "Thats not from sable, sable use db",
    "hey moron, i said check sable. sable done use that folder",
    "its zero now because there is no thinking, but is it saving or not from the program",
    "Then keep everything same as usual just save in per turn with option 2",
    "now check the test_embed when laoding my prompt make sure only propmt with other 30char is loaded",
    "save the full prompt in hte benchmark.txt, i cant see the full prompt",
    "In this conversation is any relevant context passing to you with my prompt",
    "have you ever got one or not? like now when i am talking about test_embed.py or before",
]


def main() -> None:
    print(f"Model: {MODEL} | Threshold: {THRESHOLD}")
    print(f"Loading searcher...")

    searcher = MemorySearcher()
    searcher.set_model(MODEL)
    searcher.set_thresholds({MODEL: THRESHOLD})
    searcher._ensure_loaded()

    print(f"Memory entries loaded: {len(searcher._entries)}")
    print(f"Effective threshold: {searcher.threshold}")
    print("=" * 80)

    for i, prompt in enumerate(RECENT_PROMPTS, 1):
        print(f"\n[{i}] \"{prompt}\"")

        # Get raw scores (bypass threshold)
        results = searcher.search(prompt, top_k=5, threshold=0.0)
        if not results:
            print("    ❌ No results at all")
            continue

        passed = [r for r in results if r["score"] >= THRESHOLD]
        print(f"    Top-5 scores: {', '.join(f'{r['score']:.4f}' for r in results)}")
        if passed:
            print(f"    ✅ {len(passed)} would pass threshold:")
            for r in passed:
                print(f"       [{r['score']:.4f}] {r['key']}: {r['value'][:80]}")
        else:
            print(f"    ❌ None pass {THRESHOLD} (best: {results[0]['score']:.4f}, gap: {THRESHOLD - results[0]['score']:.4f})")
            print(f"       Best match: {results[0]['key']}: {results[0]['value'][:80]}")


if __name__ == "__main__":
    main()
