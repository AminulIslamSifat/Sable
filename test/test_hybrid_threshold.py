
"""Find the right hybrid-score threshold per model using 50 real prompts vs real Memory.json."""

import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.memory_search import (
    MemorySearcher, _tokenize, _keyword_score,
    VECTOR_WEIGHT, KEYWORD_WEIGHT, PROTECTED_BOOST,
)

DB_PATH = Path(__file__).resolve().parent.parent / "sable.db"

MODELS = [
    "jinaai/jina-embeddings-v2-small-en",
    "snowflake/snowflake-arctic-embed-xs",
    "BAAI/bge-small-en-v1.5",
    "thenlper/gte-base",
]


def load_prompts(n: int = 50) -> list[str]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT content FROM messages WHERE role = 'user' ORDER BY id DESC LIMIT ?", (n * 2,)
    ).fetchall()
    conn.close()
    prompts: list[str] = []
    for row in rows:
        text = row["content"] or ""
        if text.startswith("[") and "\n" in text[:25]:
            text = text.split("\n", 1)[1]
        text = text.strip()
        if text and len(text) >= 30:
            prompts.append(text)
        if len(prompts) >= n:
            break
    return prompts


def run_model(model_name: str, prompts: list[str]) -> None:
    print(f"\n{'═' * 80}")
    print(f"  MODEL: {model_name}")
    print(f"{'═' * 80}")

    searcher = MemorySearcher()
    # Reset singleton state for fresh model load
    searcher._model = None
    searcher._normed_vectors = None
    searcher._entries = []
    searcher._entry_meta = []
    searcher._entry_tokens = []
    searcher.set_model(model_name)

    t0 = time.perf_counter()
    searcher._ensure_loaded()
    load_s = time.perf_counter() - t0
    print(f"  Loaded {len(searcher._entries)} memory entries in {load_s:.2f}s")

    if not searcher._entries or searcher._normed_vectors is None:
        print("  ❌ No entries loaded, skipping")
        return

    all_top1: list[float] = []
    all_top3: list[float] = []
    all_scores_flat: list[float] = []

    for i, prompt in enumerate(prompts):
        # Replicate the exact hybrid scoring from memory_search.py
        q_vec = np.array(list(searcher._model.embed([prompt]))[0], dtype="float32")
        q_norm_val = np.linalg.norm(q_vec)
        q_n = q_vec if q_norm_val == 0 else q_vec / q_norm_val
        vector_scores = searcher._normed_vectors @ q_n

        query_tokens = _tokenize(prompt)
        keyword_scores = np.array(
            [_keyword_score(query_tokens, et) for et in searcher._entry_tokens],
            dtype="float32",
        )
        scores = VECTOR_WEIGHT * vector_scores + KEYWORD_WEIGHT * keyword_scores

        for j, meta in enumerate(searcher._entry_meta):
            if meta["category"] == "protected":
                scores[j] += PROTECTED_BOOST

        ranked = np.argsort(-scores)
        top1 = float(scores[ranked[0]])
        top3 = float(scores[ranked[2]]) if len(ranked) >= 3 else top1
        all_top1.append(top1)
        all_top3.append(top3)
        all_scores_flat.extend(scores.tolist())

        p_short = prompt.replace("\n", " ")[:70]
        best_key = searcher._entry_meta[ranked[0]]["key"][:40]
        print(f"  [{i+1:>2}] top1={top1:.4f} top3={top3:.4f}  \"{p_short}\"")
        print(f"       → best: {best_key}")

    all_top1.sort()
    all_top3.sort()
    n = len(all_top1)

    print(f"\n{'─' * 80}")
    print(f"  📐 TOP-1 SCORE DISTRIBUTION ({n} prompts)")
    print(f"{'─' * 80}")
    print(f"  min={all_top1[0]:.4f}  p10={all_top1[n//10]:.4f}  p25={all_top1[n//4]:.4f}  "
          f"median={all_top1[n//2]:.4f}  p75={all_top1[3*n//4]:.4f}  "
          f"p90={all_top1[9*n//10]:.4f}  max={all_top1[-1]:.4f}")
    print(f"  mean={np.mean(all_top1):.4f}  std={np.std(all_top1):.4f}")

    print(f"\n  📐 TOP-3 (worst of top-3) DISTRIBUTION")
    print(f"  min={all_top3[0]:.4f}  p25={all_top3[n//4]:.4f}  "
          f"median={all_top3[n//2]:.4f}  p75={all_top3[3*n//4]:.4f}  max={all_top3[-1]:.4f}")

    # Suggest thresholds at different strictness levels
    noise = all_top1[n // 4]       # p25 — bottom 25% are likely noise
    typical = all_top1[n // 2]     # median
    loose = round(noise, 3)
    balanced = round((noise + typical) / 2, 3)
    strict = round(typical, 3)

    print(f"\n  💡 SUGGESTED THRESHOLDS (hybrid: {VECTOR_WEIGHT}v + {KEYWORD_WEIGHT}k)")
    print(f"     Loose    (p25):              {loose:.3f}  — catches most, some noise")
    print(f"     Balanced (p25+median / 2):   {balanced:.3f}  — recommended")
    print(f"     Strict   (median):           {strict:.3f}  — only strong matches")
    print(f"{'═' * 80}")


def main() -> None:
    prompts = load_prompts(50)
    print(f"Loaded {len(prompts)} prompts (≥30 chars) from sable.db")

    for model in MODELS:
        try:
            run_model(model, prompts)
        except Exception as e:
            print(f"\n  ❌ {model}: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
