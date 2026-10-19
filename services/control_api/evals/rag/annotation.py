"""Human annotation workflow: export a review queue, measure agreement, apply labels.

1. ``python -m evals.rag.annotation export --annotator A`` writes
   ``labels/queue_<A>.csv`` (one row per case that needs review, see
   difficulty.py for the triggers). Two annotators label independently.
2. Each annotator fills: ``evidence_ok`` (Y/N: the gold evidence really answers
   the query and nothing else in scope does it better), ``answerable_ok`` (Y/N),
   ``human_difficulty`` (easy/medium/hard, judged with the guideline below),
   ``notes``.
3. ``python -m evals.rag.annotation agree labels/queue_A.csv labels/queue_B.csv``
   prints Cohen's kappa (unweighted for Y/N, linear-weighted for the ordinal
   difficulty). Proceed only when kappa >= 0.61 ("substantial", Landis & Koch 1977);
   otherwise refine the guideline and relabel the disagreements.
4. Adjudicated rows go to ``labels/human_overrides.csv`` (same columns) and win
   over the automatic difficulty on the next ``python -m evals.rag.difficulty --write``.

Difficulty guideline for annotators (mirrors the three indicators):
  easy   — one passage; the query reuses the passage's own words or identifiers.
  medium — two passages, OR paraphrase / other language, OR a table row whose
           meaning depends on the column header.
  hard   — three or more passages, OR combines paraphrase with multi-hop, OR the
           evidence is degraded (OCR) or contradicted by an obsolete edition.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

from evals.rag import difficulty as d
from evals.rag import harness as h

FIELDS = ["id", "track", "category", "query", "evidence", "auto_difficulty", "signals",
          "review_reasons", "evidence_ok", "answerable_ok", "human_difficulty", "notes"]
ORDINAL = {"easy": 0, "medium": 1, "hard": 2}


def export_queue(annotator: str, out_dir: Path = d.LABELS_DIR) -> Path:
    labels = d.load_labels()
    cases = {c["id"]: c for c in h.load_cases()}
    if any(lab["track"] == "general" and lab["review_reasons"] for lab in labels.values()):
        from evals.rag import general_track as g

        passages, gcases = g.build_general_track(g.load_crud())
        for c in gcases:
            c["gold_passages"] = [passages[p][:300] for p in c["gold_passages"]]
            cases[c["id"]] = c
    out = out_dir / f"queue_{annotator}.csv"
    out_dir.mkdir(exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for lab in labels.values():
            if not lab["review_reasons"]:
                continue
            case = cases.get(lab["id"], {})
            w.writerow({
                "id": lab["id"], "track": lab["track"], "category": lab["category"],
                "query": case.get("query", ""),
                "evidence": json.dumps(case.get("evidence", case.get("gold_passages", [])), ensure_ascii=False),
                "auto_difficulty": lab["difficulty"],
                "signals": json.dumps(lab["signals"]),
                "review_reasons": ";".join(lab["review_reasons"]),
                "evidence_ok": "", "answerable_ok": "", "human_difficulty": "", "notes": "",
            })
    return out


def cohen_kappa(a: list[str], b: list[str], *, weights: dict[str, int] | None = None) -> float:
    """Cohen's kappa (1960); with ``weights`` (ordinal codes) the linear-weighted kappa (Cohen 1968)."""
    if len(a) != len(b) or not a:
        raise ValueError("need two equal-length, non-empty label lists")
    cats = sorted(set(a) | set(b), key=lambda c: (weights or {}).get(c, 0))
    n = len(a)
    if weights:
        span = max(weights[c] for c in cats) - min(weights[c] for c in cats) or 1

        def dis(x, y):
            return abs(weights[x] - weights[y]) / span
    else:

        def dis(x, y):
            return float(x != y)

    observed = sum(dis(x, y) for x, y in zip(a, b, strict=True)) / n
    pa, pb = Counter(a), Counter(b)
    expected = sum(pa[x] * pb[y] * dis(x, y) for x in cats for y in cats) / (n * n)
    return 1.0 if expected == 0 else 1 - observed / expected


def agreement(file_a: Path, file_b: Path) -> dict[str, float]:
    def read(p):
        with p.open(encoding="utf-8") as fh:
            return {r["id"]: r for r in csv.DictReader(fh)}

    ra, rb = read(file_a), read(file_b)
    common = sorted(set(ra) & set(rb))
    out = {"n": len(common)}
    for field in ("evidence_ok", "answerable_ok"):
        pairs = [(ra[i][field], rb[i][field]) for i in common if ra[i][field] and rb[i][field]]
        if pairs:
            out[f"kappa_{field}"] = cohen_kappa([x for x, _ in pairs], [y for _, y in pairs])
    pairs = [(ra[i]["human_difficulty"], rb[i]["human_difficulty"]) for i in common
             if ra[i]["human_difficulty"] in ORDINAL and rb[i]["human_difficulty"] in ORDINAL]
    if pairs:
        out["weighted_kappa_difficulty"] = cohen_kappa(
            [x for x, _ in pairs], [y for _, y in pairs], weights=ORDINAL)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("export")
    ex.add_argument("--annotator", required=True)
    ag = sub.add_parser("agree")
    ag.add_argument("a", type=Path)
    ag.add_argument("b", type=Path)
    args = ap.parse_args()
    if args.cmd == "export":
        print(export_queue(args.annotator))
    else:
        print(json.dumps(agreement(args.a, args.b), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
