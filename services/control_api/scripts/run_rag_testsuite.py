"""Staged RAG test suite: T1 parse → T2 chunk → T3 retrieval (vertical + general) → T4 generation.

Every T3 number is reported per stratum (track × difficulty) with n, standard
error, 95% CI, and — for the system comparison — the minimum detectable effect
and the number of questions needed, so a reader can tell whether the data
volume supports the conclusion.

From ``services/control_api``::

    python -m evals.rag.difficulty --write     # once per dataset version
    python scripts/run_rag_testsuite.py --out suite.json
    python scripts/run_rag_testsuite.py --answers answers.jsonl   # score a generator (T4)
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.rag import difficulty as d
from evals.rag import general_track as g
from evals.rag import harness as h
from evals.rag import metrics as m
from evals.rag import (
    power,
    stages,
)

TARGET_DELTA = 0.05
LEVELS = ["easy", "medium", "hard", "all"]


def f(x, nd=3):
    return "—" if x is None or math.isnan(x) else f"{x:.{nd}f}"


def compare(rows_a: list[dict], rows_b: list[dict], labels: dict, metric: str, cluster_of) -> list[dict]:
    """Paired comparison B − A per difficulty stratum."""
    out = []
    for level in LEVELS:
        ids = [r["id"] for r in rows_a if r.get("answerable", True) and metric in r
               and (level == "all" or labels.get(r["id"], {}).get("difficulty_prior") == level)]
        if len(ids) < 2:
            out.append({"stratum": level, "n": len(ids)})
            continue
        ra = {r["id"]: r[metric] for r in rows_a if metric in r}
        rb = {r["id"]: r[metric] for r in rows_b if metric in r}
        a = [ra[i] for i in ids]
        b = [rb[i] for i in ids]
        pr = power.paired(b, a)
        diffs = [y - x for x, y in zip(a, b, strict=True)]
        se_c = power.clustered_se(diffs, [cluster_of(i) for i in ids])
        omega = pr["omega"] if pr["omega"] > 0 else 1e-9
        deff = power.design_effect(se_c, pr["se"]) if pr["se"] else 1.0
        inflate = max(1.0, deff)  # never let clustering shrink the error bar
        se_use = pr["se"] * inflate**0.5
        mde = power.mde_paired(omega, len(ids)) * inflate**0.5
        n05 = math.ceil(power.required_n_paired(omega, 0.05) * inflate)
        n10 = math.ceil(power.required_n_paired(omega, 0.10) * inflate)
        k = len({cluster_of(i) for i in ids})
        m_bar = len(ids) / k
        icc = min(1.0, max(0.0, (deff - 1) / (m_bar - 1))) if m_bar > 1 else 0.0
        # design with at most 5 questions per independent section: DEFF' = 1 + 4*icc (Kish)
        k05 = math.ceil(power.required_n_paired(omega, 0.05) * (1 + 4 * icc) / 5)
        p_val = m.paired_randomization_test(b, a, trials=5000)
        if p_val < 0.05 and abs(pr["delta"]) > 1.96 * se_use:
            verdict = "差异显著"
        elif mde <= TARGET_DELTA:
            verdict = f"可排除 ≥{TARGET_DELTA:.2f} 的差异"
        else:
            verdict = f"样本不足：按每节≤5题需 {k05} 个独立章节"
        out.append({
            "stratum": level, "n": len(ids), "clusters": len({cluster_of(i) for i in ids}),
            "mean_a": power.mean(a), "se_a": power.se_mean(a), "mean_b": power.mean(b), "se_b": power.se_mean(b),
            "delta": pr["delta"], "se_iid": pr["se"], "se_clustered": se_c, "se_used": se_use,
            "deff": deff, "icc": icc, "sections_for_0.05": k05, "p_randomization": p_val,
            "mde_80": mde, "n_for_0.05": n05, "n_for_0.10": n10, "verdict": verdict,
        })
    return out


def print_compare(title: str, rows: list[dict]) -> None:
    print(f"\n### {title}\n")
    print("| 难度 | n | 簇数 | A 均值±SE | B 均值±SE | Δ(B−A) | SE(采用) | DEFF | ICC | p | MDE(80%) | 结论 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        if "delta" not in r:
            print(f"| {r['stratum']} | {r['n']} | | | | | | | | | | 样本过少 |")
            continue
        print(f"| {r['stratum']} | {r['n']} | {r['clusters']} | {f(r['mean_a'])}±{f(r['se_a'])} | "
              f"{f(r['mean_b'])}±{f(r['se_b'])} | {r['delta']:+.3f} | {f(r['se_used'])} | {f(r['deff'], 2)} | {f(r['icc'], 2)} | "
              f"{f(r['p_randomization'], 4)} | {f(r['mde_80'])} | {r['verdict']} |")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path)
    ap.add_argument("--answers", type=Path, help="JSONL {id, answer} from a generator, for T4")
    ap.add_argument("--per-level", type=int, default=200)
    args = ap.parse_args()

    manifest, cases = h.load_manifest(), h.load_cases()
    problems = h.validate_gold(manifest, cases)
    if problems:
        print("Gold set problems:\n  " + "\n  ".join(problems))
        return 2
    parsed = h.parse_corpus(manifest)
    labels = d.load_labels()
    if not labels:
        print("labels/difficulty.jsonl missing — run: python -m evals.rag.difficulty --write")
        return 2
    report: dict = {}

    # T1 parse ---------------------------------------------------------------
    report["T1_parse"] = stages.parse_stage(manifest, parsed, cases)
    report["T1_ocr"] = {k: v.get("ocr_cer") for k, v in h.parse_stats(manifest, parsed).items() if "ocr_cer" in v}
    print("## T1 解析阶段：证据事实存活率\n")
    print("| 来源格式 | 事实数 | 原文存活率 ± SE | 归一化后存活率 ± SE |")
    print("|---|---|---|---|")
    for fmt, r in report["T1_parse"].items():
        print(f"| {fmt} | {r['n_facts']} | {f(r['strict_survival'])} ± {f(r['strict_se'])} | "
              f"{f(r['loose_survival'])} ± {f(r['loose_se'])} |")
    print(f"\nOCR CER: {report['T1_ocr']}")

    # T2 chunk + T3 vertical retrieval --------------------------------------------
    r0 = h.evaluate_run(h.RunConfig("R0 baseline BM25", "baseline"), manifest, parsed, cases)
    r4 = h.evaluate_run(h.RunConfig("R4 structured+current BM25", "structured", current_only=True),
                        manifest, parsed, cases)
    report["T2_chunk"] = {r["config"]["name"]: {**r["chunk_stats"],
                          "self_contained@5": r["summary"]["overall"].get("self_contained@5")} for r in (r0, r4)}
    print("\n## T2 分块阶段\n")
    for name, st in report["T2_chunk"].items():
        print(f"- {name}: chunks={st['n_chunks']}, p50={st['tokens_p50']}, p95={st['tokens_p95']}, "
              f"<16tok={f(st['share_under_16_tokens'])}, self_contained@5={f(st['self_contained@5'])}")


    # Cluster = the manual section that holds the first evidence group (Miller 2024: questions
    # sharing a passage are correlated). Categories are fixed strata, not clusters.
    s_chunks = h.build_index_chunks(manifest, parsed, "structured")
    s_keys = [h.match_key(ch.content) for ch in s_chunks]
    section_of: dict[str, str] = {}
    for c in cases:
        hits = [i for i, ch in enumerate(s_chunks) if ch.device_model_id == c["device_model_id"]
                and c["evidence"] and any(h.match_key(a) in s_keys[i] for a in c["evidence"][0])]
        hits.sort(key=lambda i: not s_chunks[i].is_current)
        section_of[c["id"]] = s_chunks[hits[0]].content.split("\n", 1)[0] if hits else c["id"]

    def v_cluster(cid):
        return section_of[cid]

    print("\n## T3 检索阶段 · 垂直行业轨（工业手册）")
    dist = {lv: sum(1 for c in cases if labels.get(c["id"], {}).get("difficulty") == lv) for lv in LEVELS[:3]}
    prior = {lv: sum(1 for c in cases if labels.get(c["id"], {}).get("difficulty_prior") == lv) for lv in LEVELS[:3]}
    print(f"\n综合难度分布（三指标）：{dist}；先验难度分布（D1+D2，用于下表分层）：{prior}"
          f"（不可答 {sum(not c['answerable'] for c in cases)} 题不计入召回）")
    report["T3_vertical"] = {
        "recall@5": compare(r0["per_query"], r4["per_query"], labels, "recall@5", v_cluster),
        "full@5": compare(r0["per_query"], r4["per_query"], labels, "full@5", v_cluster),
    }
    print_compare("recall@5：A = R0 仓库现状，B = R4 结构化分块 + 仅当前版", report["T3_vertical"]["recall@5"])

    # T3 general retrieval --------------------------------------------------------
    passages, gcases = g.build_general_track(g.load_crud(), per_level=args.per_level)
    gres = g.evaluate_general(passages, gcases, {
        "bm25u": g.bm25_ranker(passages, h.tokenize_unigram), "bm25": g.bm25_ranker(passages)})
    event = {c["id"]: c["id"].split("-", 2)[-1] for c in gcases}  # same CRUD event across 1/2/3-doc variants
    report["T3_general"] = {
        "full@5": compare(gres["bm25u"], gres["bm25"], labels, "full@5", lambda i: event[i]),
        "recall@5": compare(gres["bm25u"], gres["bm25"], labels, "recall@5", lambda i: event[i]),
    }
    print(f"\n## T3 检索阶段 · 通用轨（CRUD-RAG 中文新闻，{len(passages)} 段落，{len(gcases)} 题）")
    print_compare("full@5：A = BM25 单字，B = BM25 单字+双字", report["T3_general"]["full@5"])

    # T4 generation -----------------------------------------------------------------
    gen = stages.build_generation_cases(manifest, parsed, cases)
    stages.write_generation_cases(gen)
    sanity = stages.score_generation(gen, {c["id"]: c["reference_answer"] for c in gen})
    report["T4_generation"] = {"n_cases": len(gen), "sanity_reference_answers": sanity}
    print(f"\n## T4 生成阶段\n\n{len(gen)} 题（{sum(c['must_refuse'] for c in gen)} 题应拒答），"
          f"参考答案自检：{json.dumps(sanity, ensure_ascii=False)}")
    if args.answers:
        with args.answers.open(encoding="utf-8") as fh:
            answers = {r["id"]: r["answer"] for r in map(json.loads, fh) if r}
        report["T4_generation"]["scored"] = stages.score_generation(gen, answers)
        print(f"生成器得分：{json.dumps(report['T4_generation']['scored'], ensure_ascii=False)}")

    # sample size reference ----------------------------------------------------------
    print("\n## 样本量参考\n")
    for e in (0.10, 0.05, 0.03):
        print(f"- 单系统比例指标 ±{e:.2f}（95%，p=0.5 最坏情况）需 n = {power.required_n_proportion(e)}")
    if args.out:
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        print(f"\nfull report: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
