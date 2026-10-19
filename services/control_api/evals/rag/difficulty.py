"""Three-indicator difficulty grading (易 / 中 / 难) for every test case.

Indicators (each mapped to a level 0 / 1 / 2):

D1 reasoning hops   h = number of evidence groups (vertical) or gold passages
                        (general) the answer needs.          (HotpotQA, MultiHop-RAG, FRAMES)
                        h <= 1 -> 0, h = 2 -> 1, h >= 3 -> 2
D2 lexical gap      o = min over evidence groups of the best |T(q) ∩ T(p)| / |T(q)|
                        between the query terms and a passage holding that group
                        (folded text, words/codes + CJK bigrams).  (BEIR lexical-overlap
                        analysis; BRIGHT: relevance not detectable by keyword overlap)
                        o >= 0.6 -> 0, 0.3 <= o < 0.6 -> 1, o < 0.3 -> 2
D3 empirical        p = share of a reference panel of retrieval systems that
   difficulty           fully cover the evidence in their top 5 (classical test
                        theory item difficulty; HotpotQA split easy/medium/hard by
                        whether a baseline model solved the item; tinyBenchmarks/IRT)
                        p >= 0.8 -> 0, 0.3 <= p < 0.8 -> 1, p < 0.3 -> 2

Composite S = D1 + D2 + D3 in [0, 6]:  S <= 1 easy, 2..3 medium, >= 4 hard.

Unanswerable cases invert D2/D3 (a near-miss is what makes rejection hard):
D1 = 0; D2 from the best lexical overlap with ANY in-scope passage (o >= 0.6 -> 2,
0.3..0.6 -> 1, else 0); D3 from the share of panel systems whose top-1 score is
at least the median top-1 score of answerable cases (false-accept rate).

Human review is requested when the a-priori indicators and the observed one
contradict each other (D1+D2 <= 1 but D3 = 2: looks trivial yet every system
fails, often a gold-label or parsing error; or D1+D2 >= 3 but D3 = 0: looks hard
yet every system passes, often a shortcut), for every unanswerable case (confirm nothing in scope answers it,
as MultiHop-RAG/FRAMES did for null questions), and for a fixed 10% random
sample of template cases. Human labels in ``labels/human_overrides.csv`` win.

Run:  python -m evals.rag.difficulty --write
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
from collections import Counter
from statistics import median

from evals.rag import harness as h

LABELS_DIR = h.RAG_DIR / "labels"
DIFFICULTY_FILE = LABELS_DIR / "difficulty.jsonl"
OVERRIDES_FILE = LABELS_DIR / "human_overrides.csv"
LEVEL_NAMES = {0: "easy", 1: "medium", 2: "hard"}


def hop_level(hops: int) -> int:
    return 0 if hops <= 1 else 1 if hops == 2 else 2


def gap_level(overlap: float) -> int:
    return 0 if overlap >= 0.6 else 1 if overlap >= 0.3 else 2


def empirical_level(pass_rate: float) -> int:
    return 0 if pass_rate >= 0.8 else 1 if pass_rate >= 0.3 else 2


def composite(levels: list[int]) -> str:
    s = sum(levels)
    return "easy" if s <= 1 else "medium" if s <= 3 else "hard"


def prior_grade(prior: int) -> str:
    return "easy" if prior == 0 else "medium" if prior <= 2 else "hard"


def overlap(query: str, passage: str) -> float:
    q = h.content_terms(query)
    if not q:
        return 0.0
    return len(q & h.content_terms(passage)) / len(q)


def vertical_panel() -> list[h.RunConfig]:
    panel = [
        h.RunConfig("P1 baseline BM25", "baseline", "bm25"),
        h.RunConfig("P2 structured BM25", "structured", "bm25"),
        h.RunConfig("P3 structured BM25-unigram", "structured", "bm25u"),
    ]
    if os.environ.get("RAG_EVAL_EMBEDDING_MODEL") or os.environ.get("DEVICE_DOCUMENT_EMBEDDING_MODEL"):
        panel += [
            h.RunConfig("P4 structured dense", "structured", "dense"),
            h.RunConfig("P5 structured hybrid", "structured", "hybrid"),
        ]
    return panel


def grade_vertical(manifest: dict, parsed: dict, cases: list[dict]) -> list[dict]:
    panel = vertical_panel()
    runs = [h.evaluate_run(cfg, manifest, parsed, cases) for cfg in panel]
    by_id = [{r["id"]: r for r in run["per_query"]} for run in runs]
    ans_top = [median(r["top_score"] for r in run["per_query"] if r["answerable"]) for run in runs]
    chunks = h.build_index_chunks(manifest, parsed, "structured")
    keys = [h.match_key(c.content) for c in chunks]
    out = []
    for case in cases:
        scope = [i for i, c in enumerate(chunks) if c.device_model_id == case["device_model_id"]]
        if case["answerable"]:
            per_group = []
            for grp in case["evidence"]:
                holders = [i for i in scope if any(h.match_key(a) in keys[i] for a in grp)]
                per_group.append(max((overlap(case["query"], chunks[i].content) for i in holders), default=0.0))
            o = min(per_group) if per_group else 0.0
            p = sum(b[case["id"]]["full@5"] for b in by_id) / len(by_id)
            levels = [hop_level(len(case["evidence"])), gap_level(o), empirical_level(p)]
        else:
            o = max((overlap(case["query"], chunks[i].content) for i in scope), default=0.0)
            false_accept = sum(
                b[case["id"]]["top_score"] >= t for b, t in zip(by_id, ans_top, strict=True)
            ) / len(by_id)
            p = 1 - false_accept  # "pass" = the panel correctly scores it below typical answerable queries
            levels = [0, 2 - gap_level(o), empirical_level(p)]
        out.append(_label(case, levels, o, p, len(case["evidence"]), len(panel)))
    return out


def grade_general(passages: dict[str, str], cases: list[dict], panel_rankers: dict) -> list[dict]:
    from evals.rag import general_track as g

    results = g.evaluate_general(passages, cases, panel_rankers)
    out = []
    for i, case in enumerate(cases):
        o = min(overlap(case["query"], passages[pid]) for pid in case["gold_passages"])
        p = sum(rows[i]["full@5"] for rows in results.values()) / len(results)
        hops = len(case["gold_passages"])
        levels = [hop_level(hops), gap_level(o), empirical_level(p)]
        out.append(_label(case, levels, o, p, hops, len(panel_rankers)))
    return out


def _label(case: dict, levels: list[int], o: float, p: float, hops: int, panel_size: int) -> dict:
    reasons = []
    prior = levels[0] + levels[1]  # what the item looks like before any system runs
    if (prior <= 1 and levels[2] == 2) or (prior >= 3 and levels[2] == 0):
        reasons.append("indicators_disagree")
    if not case.get("answerable", True):
        reasons.append("confirm_unanswerable")
    return {
        "id": case["id"],
        "track": case.get("track", "vertical"),
        "category": case["category"],
        "signals": {"hops": hops, "lexical_overlap": round(o, 3), "panel_pass_rate": round(p, 3)},
        "levels": {"D1_hops": levels[0], "D2_lexical_gap": levels[1], "D3_empirical": levels[2]},
        "difficulty": composite(levels),
        # A-priori grade (D1 + D2 only). Use THIS to stratify system comparisons: D3 is
        # measured with retrieval systems, so stratifying by it would favour whichever
        # system resembles the panel (the reason HotpotQA graded with a separate baseline).
        "difficulty_prior": prior_grade(levels[0] + levels[1]),
        "difficulty_source": "auto",
        "provisional": panel_size < 4,  # lexical-only panel; recompute once a dense model is in the panel
        "review_reasons": reasons,
    }


def sample_template_audit(labels: list[dict], cases: list[dict], share: float = 0.1, seed: int = 11) -> None:
    template_ids = sorted(c["id"] for c in cases if c.get("source") == "template")
    rng = random.Random(seed)
    audit = set(rng.sample(template_ids, max(1, round(share * len(template_ids))))) if template_ids else set()
    for lab in labels:
        if lab["id"] in audit:
            lab["review_reasons"].append("template_audit")


def cap_review_queue(labels: list[dict], cap: int = 30, seed: int = 5) -> None:
    """Acceptance sampling: when a (track, reason) bucket exceeds ``cap`` items, review a
    random ``cap`` of them and estimate the bucket's label-error rate from that sample."""
    buckets: dict[tuple[str, str], list[dict]] = {}
    for lab in labels:
        for reason in lab["review_reasons"]:
            buckets.setdefault((lab["track"], reason), []).append(lab)
    rng = random.Random(seed)
    for (_track, reason), items in buckets.items():
        if len(items) > cap:
            keep = {lab["id"] for lab in rng.sample(sorted(items, key=lambda x: x["id"]), cap)}
            for lab in items:
                if lab["id"] not in keep:
                    lab["review_reasons"].remove(reason)
                    lab.setdefault("audited_by_sample", []).append(reason)


def apply_overrides(labels: list[dict]) -> list[dict]:
    if not OVERRIDES_FILE.exists():
        return labels
    with OVERRIDES_FILE.open(encoding="utf-8") as fh:
        overrides = {r["id"]: r for r in csv.DictReader(fh) if r.get("human_difficulty")}
    for lab in labels:
        o = overrides.get(lab["id"])
        if o:
            lab["difficulty"] = o["human_difficulty"].strip()
            lab["difficulty_source"] = "human"
    return labels


def load_labels() -> dict[str, dict]:
    if not DIFFICULTY_FILE.exists():
        return {}
    with DIFFICULTY_FILE.open(encoding="utf-8") as fh:
        return {r["id"]: r for r in (json.loads(line) for line in fh if line.strip())}


def main() -> int:
    from evals.rag import general_track as g

    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--per-level", type=int, default=200)
    args = ap.parse_args()

    manifest = h.load_manifest()
    parsed = h.parse_corpus(manifest)
    cases = h.load_cases()
    labels = grade_vertical(manifest, parsed, cases)
    sample_template_audit(labels, cases)

    passages, gcases = g.build_general_track(g.load_crud(), per_level=args.per_level)
    panel = {"bm25": g.bm25_ranker(passages), "bm25u": g.bm25_ranker(passages, h.tokenize_unigram)}
    labels += grade_general(passages, gcases, panel)
    cap_review_queue(labels)
    labels = apply_overrides(labels)

    dist = Counter((lab["track"], lab["difficulty"]) for lab in labels)
    for (track, diff), n in sorted(dist.items()):
        print(f"{track:9s} {diff:7s} {n}")
    print("needs human review:", sum(1 for lab in labels if lab["review_reasons"]))
    if args.write:
        LABELS_DIR.mkdir(exist_ok=True)
        with DIFFICULTY_FILE.open("w", encoding="utf-8") as fh:
            for lab in labels:
                fh.write(json.dumps(lab, ensure_ascii=False) + "\n")
        print(f"written {DIFFICULTY_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
