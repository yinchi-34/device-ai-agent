"""Run the offline RAG retrieval evaluation.

From ``services/control_api``::

    python scripts/run_rag_evals.py                     # BM25 runs, offline
    python scripts/run_rag_evals.py --dense             # + dense / hybrid runs
    python scripts/run_rag_evals.py --out report.json   # full per-query report

Dense runs need an OpenAI-compatible embedding endpoint, e.g. the local
Ollama ``bge-m3`` from ``.env.example``::

    RAG_EVAL_EMBEDDING_BASE_URL=http://localhost:11434/v1 \
    RAG_EVAL_EMBEDDING_MODEL=bge-m3 python scripts/run_rag_evals.py --dense
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.rag import harness as h
from evals.rag import metrics as m

BM25_RUNS = [
    h.RunConfig("R0 baseline (repo chunker) + BM25", "baseline"),
    h.RunConfig("R1 baseline + retried run duplicates", "baseline", retry_runs=2),
    h.RunConfig("R2 baseline + duplicates + dedupe", "baseline", retry_runs=2, dedupe=True),
    h.RunConfig("R3 structured chunker + BM25", "structured"),
    h.RunConfig("R4 structured + current edition only", "structured", current_only=True),
    h.RunConfig("D1 baseline, UNSCOPED (diagnostic)", "baseline", scoped=False),
    h.RunConfig("D2 structured, UNSCOPED (diagnostic)", "structured", scoped=False),
]
DENSE_RUNS = [
    h.RunConfig("R5 baseline + dense", "baseline", retriever="dense"),
    h.RunConfig("R6 structured + dense", "structured", retriever="dense"),
    h.RunConfig("R7 structured + hybrid RRF", "structured", retriever="hybrid"),
    h.RunConfig("R8 structured + hybrid + current only", "structured", retriever="hybrid", current_only=True),
]

HEADLINE = ["hit@5", "recall@5", "full@5", "recall@400tok", "mrr@10", "ndcg@10", "self_contained@5",
            "ctx_efficiency@5", "distractor@5", "dup_rate@5", "injection_exposed@5", "unanswerable_auroc"]


def _fmt(v) -> str:
    return "—" if v is None else f"{v:.3f}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dense", action="store_true", help="also run dense and hybrid retrievers")
    ap.add_argument("--out", type=Path, help="write the full JSON report here")
    args = ap.parse_args()

    manifest = h.load_manifest()
    cases = h.load_cases()
    problems = h.validate_gold(manifest, cases)
    if problems:
        print("Gold set problems:\n  " + "\n  ".join(problems))
        return 2
    parsed = h.parse_corpus(manifest)

    runs = BM25_RUNS + (DENSE_RUNS if args.dense else [])
    reports = [h.evaluate_run(cfg, manifest, parsed, cases) for cfg in runs]

    print(f"cases: {len(cases)} ({reports[0]['summary']['n_answerable']} answerable, "
          f"{reports[0]['summary']['n_unanswerable']} unanswerable)\n")
    print("## Parse stage\n")
    for doc_id, st in h.parse_stats(manifest, parsed).items():
        print(f"- {doc_id}: {json.dumps(st, ensure_ascii=False)}")
    print("\n## Chunk stats\n")
    for r in reports:
        print(f"- {r['config']['name']}: {json.dumps(r['chunk_stats'])}")
    print("\n## Retrieval (answerable queries; k=5 unless noted)\n")
    print("| run | " + " | ".join(HEADLINE) + " |")
    print("|---|" + "---|" * len(HEADLINE))
    for r in reports:
        o = r["summary"]["overall"]
        print(f"| {r['config']['name']} | " + " | ".join(_fmt(o.get(k)) for k in HEADLINE) + " |")

    base, best = reports[0], reports[4]
    print(f"\n## Per category: recall@5  ({base['config']['name']}  →  {best['config']['name']})\n")
    print("| category | n | reachable | recall@5 | full@5 | distractor@5 |")
    print("|---|---|---|---|---|---|")
    for cat, b in base["summary"]["by_category"].items():
        s = best["summary"]["by_category"][cat]
        def pair(k, b=b, s=s):
            return f"{_fmt(b.get(k))} → {_fmt(s.get(k))}"
        print(f"| {cat} | {b['n']} | {pair('reachable')} | {pair('recall@5')} | {pair('full@5')} | {pair('distractor@5')} |")

    a = [q.get("recall@5", 0.0) for q in base["per_query"] if q["answerable"]]
    b = [q.get("recall@5", 0.0) for q in best["per_query"] if q["answerable"]]
    p = m.paired_randomization_test(b, a)
    print(f"\nPaired randomization test on recall@5 (R4 vs R0): Δ={sum(b)/len(b)-sum(a)/len(a):+.3f}, p={p:.4f}")

    if args.out:
        args.out.write_text(json.dumps({"parse": h.parse_stats(manifest, parsed), "runs": reports},
                                       ensure_ascii=False, indent=2, default=list))
        print(f"\nfull report: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
