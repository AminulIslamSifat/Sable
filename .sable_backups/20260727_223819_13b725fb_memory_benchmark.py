#!/usr/bin/env python3
"""
memory_benchmark.py

Benchmarks embedding models on a realistic "AI assistant memory" retrieval
task: 50 memory entries, 25 natural-language user prompts, hand-labeled
ground truth for which memories each prompt should retrieve.

For each model it measures:
  - Enc ms   : avg ms to embed one memory entry (corpus encoding)
  - Qry ms   : avg ms to embed one query at request time
  - Recall%  : of the relevant memories for a query, what % were retrieved
               in the top-k results (averaged over all queries)
  - Prec%    : of the top-k retrieved results, what % were actually relevant
  - ZeroM%   : % of queries where NONE of the top-k results were relevant
               (a "total miss" - the worst failure mode for a memory system)
  - Load     : one-time model load time

Usage:
    pip install sentence-transformers torch einops
    python3 memory_benchmark.py                       # run all models
    python3 memory_benchmark.py --models bge-small-en-v1.5,all-MiniLM-L6-v2
    python3 memory_benchmark.py --top-k 3
    python3 memory_benchmark.py --mock                # no models needed,
                                                        # sanity-checks the
                                                        # harness with random
                                                        # vectors
    python3 memory_benchmark.py --device cuda

Output:
    Prints a results table to stdout and writes a timestamped .txt report
    (summary table + full per-query breakdown per model) to --output
    (default: ./benchmark_results_<timestamp>.txt)
"""

import argparse
import sys
import time
from datetime import datetime

import numpy as np

from memory_benchmark_data import MEMORIES, QUERIES

# ---------------------------------------------------------------------------
# Model registry: short name -> (HF repo id, needs trust_remote_code,
#                                 query prefix, document prefix)
#
# The prefixes matter a lot for real-world recall. Several of these models
# were trained with an "instruction" prepended to queries (and sometimes
# documents) and score noticeably worse if you skip it. Left blank ("")
# where the model doesn't use one.
# ---------------------------------------------------------------------------
MODEL_REGISTRY = {
    "snowflake-arctic-embed-xs": {
        "repo": "Snowflake/snowflake-arctic-embed-xs",
        "trust_remote_code": False,
        "query_prefix": "Represent this sentence for searching relevant passages: ",
        "doc_prefix": "",
    },
    "gte-base": {
        "repo": "thenlper/gte-base",
        "trust_remote_code": False,
        "query_prefix": "",
        "doc_prefix": "",
    },
    "nomic-embed-text-v1.5": {
        "repo": "nomic-ai/nomic-embed-text-v1.5",
        "trust_remote_code": True,
        "query_prefix": "search_query: ",
        "doc_prefix": "search_document: ",
    },
    "mxbai-embed-large-v1": {
        "repo": "mixedbread-ai/mxbai-embed-large-v1",
        "trust_remote_code": False,
        "query_prefix": "Represent this sentence for searching relevant passages: ",
        "doc_prefix": "",
    },
    "jina-embeddings-v2-small-en": {
        "repo": "jinaai/jina-embeddings-v2-small-en",
        "trust_remote_code": True,
        "query_prefix": "",
        "doc_prefix": "",
    },
    "all-MiniLM-L6-v2": {
        "repo": "sentence-transformers/all-MiniLM-L6-v2",
        "trust_remote_code": False,
        "query_prefix": "",
        "doc_prefix": "",
    },
    "bge-base-en-v1.5": {
        "repo": "BAAI/bge-base-en-v1.5",
        "trust_remote_code": False,
        "query_prefix": "Represent this sentence for searching relevant passages: ",
        "doc_prefix": "",
    },
    "bge-small-en-v1.5": {
        "repo": "BAAI/bge-small-en-v1.5",
        "trust_remote_code": False,
        "query_prefix": "Represent this sentence for searching relevant passages: ",
        "doc_prefix": "",
    },
}


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------
class RealBackend:
    """Wraps sentence-transformers for a single model."""

    def __init__(self, repo, trust_remote_code, device):
        from sentence_transformers import SentenceTransformer  # local import

        self.model = SentenceTransformer(
            repo, trust_remote_code=trust_remote_code, device=device
        )

    def encode(self, texts):
        # normalize_embeddings=True -> cosine similarity == dot product
        return self.model.encode(
            texts, normalize_embeddings=True, show_progress_bar=False
        )


class MockBackend:
    """Deterministic random embeddings, for dry-running the harness without
    any model downloads. Lets you verify the scoring logic end-to-end."""

    def __init__(self, seed, dim=128):
        self.rng = np.random.default_rng(seed)
        self.dim = dim

    def encode(self, texts):
        vecs = self.rng.normal(size=(len(texts), self.dim))
        vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
        return vecs


# ---------------------------------------------------------------------------
# Core benchmark
# ---------------------------------------------------------------------------
def cosine_topk(query_vec, corpus_vecs, k):
    # corpus_vecs assumed normalized; dot product = cosine similarity
    sims = corpus_vecs @ query_vec
    idx = np.argsort(-sims)[:k]
    return idx, sims[idx]


def run_one_model(name, backend, top_k, verbose_lines):
    mem_texts = [m["text"] for m in MEMORIES]
    mem_ids = [m["id"] for m in MEMORIES]
    cfg = MODEL_REGISTRY.get(name, {})
    doc_prefix = cfg.get("doc_prefix", "")
    query_prefix = cfg.get("query_prefix", "")

    # --- encode corpus (memories) ---
    prefixed_docs = [doc_prefix + t for t in mem_texts]
    t0 = time.perf_counter()
    corpus_vecs = np.asarray(backend.encode(prefixed_docs))
    enc_total_ms = (time.perf_counter() - t0) * 1000
    enc_ms_avg = enc_total_ms / len(mem_texts)

    # --- encode + retrieve per query ---
    recalls, precisions, zero_hits = [], [], 0
    query_times = []

    for q in QUERIES:
        prefixed_q = query_prefix + q["text"]
        t0 = time.perf_counter()
        qvec = np.asarray(backend.encode([prefixed_q])[0])
        query_times.append((time.perf_counter() - t0) * 1000)

        idx, sims = cosine_topk(qvec, corpus_vecs, top_k)
        retrieved_ids = [mem_ids[i] for i in idx]
        relevant = set(q["relevant"])
        hit = relevant.intersection(retrieved_ids)

        recall = len(hit) / len(relevant) if relevant else 0.0
        precision = len(hit) / top_k
        recalls.append(recall)
        precisions.append(precision)
        if not hit:
            zero_hits += 1

        verbose_lines.append(
            f"  [{q['id']}] \"{q['text']}\"\n"
            f"      expected : {sorted(relevant)}\n"
            f"      retrieved: {retrieved_ids}  (sims: {[round(float(s), 3) for s in sims]})\n"
            f"      recall={recall:.2f}  precision={precision:.2f}  "
            f"{'MISS' if not hit else 'hit'}\n"
        )

    qry_ms_avg = float(np.mean(query_times))
    n = len(QUERIES)
    return {
        "name": name,
        "enc_ms": enc_ms_avg,
        "qry_ms": qry_ms_avg,
        "recall_pct": 100 * float(np.mean(recalls)),
        "prec_pct": 100 * float(np.mean(precisions)),
        "zero_pct": 100 * zero_hits / n,
    }


def build_backend(name, args):
    if args.mock:
        # seed derived from name so results are stable across runs but
        # differ per "model" for a more realistic-looking mock table
        seed = abs(hash(name)) % (2**32)
        return MockBackend(seed=seed)

    cfg = MODEL_REGISTRY[name]
    return RealBackend(
        repo=cfg["repo"],
        trust_remote_code=cfg["trust_remote_code"],
        device=args.device,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--models",
        default="all",
        help="Comma-separated short names from the registry, or 'all' (default).",
    )
    parser.add_argument("--top-k", type=int, default=5, help="Top-k retrieved per query (default 5).")
    parser.add_argument("--device", default="cpu", help="cpu or cuda (default cpu).")
    parser.add_argument(
        "--output",
        default=None,
        help="Output txt path (default: benchmark_results_<timestamp>.txt).",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Skip real models entirely; use random embeddings to sanity-check the harness.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Don't print the per-query breakdown to stdout (still written to the txt file).",
    )
    args = parser.parse_args()

    if args.models == "all":
        model_names = list(MODEL_REGISTRY.keys())
    else:
        model_names = [m.strip() for m in args.models.split(",") if m.strip()]
        unknown = [m for m in model_names if m not in MODEL_REGISTRY]
        if unknown:
            print(f"Unknown model name(s): {unknown}\nAvailable: {list(MODEL_REGISTRY.keys())}", file=sys.stderr)
            sys.exit(1)

    results = []
    all_verbose = []

    for name in model_names:
        print(f"\n=== {name} ===")
        verbose_lines = [f"\n=== {name} ===  (top_k={args.top_k})\n"]
        try:
            t0 = time.perf_counter()
            backend = build_backend(name, args)
            load_s = time.perf_counter() - t0
            print(f"  loaded in {load_s:.2f}s")

            res = run_one_model(name, backend, args.top_k, verbose_lines)
            res["load_s"] = load_s
            results.append(res)
            print(
                f"  Enc {res['enc_ms']:.1f}ms | Qry {res['qry_ms']:.1f}ms | "
                f"Recall {res['recall_pct']:.1f}% | Prec {res['prec_pct']:.1f}% | "
                f"ZeroM {res['zero_pct']:.0f}%"
            )
        except Exception as e:
            print(f"  SKIPPED ({type(e).__name__}: {e})")
            verbose_lines.append(f"  SKIPPED: {type(e).__name__}: {e}\n")
        all_verbose.extend(verbose_lines)

    if not results:
        print("\nNo models ran successfully. Nothing to report.")
        sys.exit(1)

    # ---- build report text ----
    header = (
        f"Embedding model benchmark - {datetime.now().isoformat(timespec='seconds')}\n"
        f"Corpus: {len(MEMORIES)} memory entries | Queries: {len(QUERIES)} | top_k={args.top_k}\n"
        f"Mode: {'MOCK (random vectors, harness sanity-check only)' if args.mock else 'REAL (sentence-transformers)'}\n\n"
    )

    col = "{:<32}{:>8}{:>8}{:>9}{:>8}{:>8}{:>9}\n"
    table = col.format("Model", "Enc ms", "Qry ms", "Recall%", "Prec%", "ZeroM%", "Load")
    table += "-" * 88 + "\n"
    for r in sorted(results, key=lambda r: -r["recall_pct"]):
        table += col.format(
            r["name"],
            f"{r['enc_ms']:.1f}",
            f"{r['qry_ms']:.1f}",
            f"{r['recall_pct']:.1f}%",
            f"{r['prec_pct']:.1f}%",
            f"{r['zero_pct']:.0f}%",
            f"{r['load_s']:.2f}s",
        )

    full_report = header + table + "\n\nPer-query breakdown\n" + "=" * 88 + "\n" + "".join(all_verbose)

    out_path = args.output or f"benchmark_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    with open(out_path, "w") as f:
        f.write(full_report)

    print("\n" + table)
    print(f"Full report written to: {out_path}")


if __name__ == "__main__":
    main()