"""Stage-specific test sets (T1 parse, T4 generation) derived from the gold cases.

T1 parse      — every evidence string must survive parsing. ``strict`` compares
                the raw extracted text (what the index really sees); ``loose``
                compares after NFKC + radical folding. strict < loose means a
                normalisation bug; loose < 1 means content was lost (OCR, layout).
T2 chunk      — self_contained@5 and chunk statistics (harness.evaluate_run).
T3 retrieval  — queries.jsonl (vertical) + general_track (CRUD-RAG).
T4 generation — generation_cases.jsonl: query, gold context, reference answer,
                ``key_facts`` that a correct answer must state, ``must_refuse``
                for unanswerable cases. Key facts are auto-extracted drafts and
                need human verification (``annotation_status``).
T5 end-to-end — evals/diagnosis/cases.json (existing agent evaluation).
"""

from __future__ import annotations

import json
import re
from collections import defaultdict

from evals.rag import harness as h
from evals.rag import power

GEN_FILE = h.RAG_DIR / "generation_cases.jsonl"
_STRICT_RE = re.compile(r"[\s|]+")


def strict_key(text: str) -> str:
    return _STRICT_RE.sub("", text.casefold())


def parse_stage(manifest: dict, parsed: dict, cases: list[dict]) -> dict:
    docs = {d["doc_id"]: d for d in manifest["documents"]}
    gt = manifest.get("ocr_ground_truth", {})
    raw = {k: "\n".join(b.content for b in p.blocks) for k, p in parsed.items()}
    ocr_text = {k: "\n".join("\n".join(v) for v in gt.get(k, {}).values()) for k in docs}
    facts: dict[tuple[str, str], str] = {}
    for c in cases:
        for grp in c["evidence"]:
            alt = grp[0]
            for doc_id, d in docs.items():
                if d["device_model_id"] != c["device_model_id"]:
                    continue
                in_ocr = h.match_key(alt) in h.match_key(ocr_text[doc_id])
                in_doc = h.match_key(alt) in h.match_key(raw[doc_id])
                if in_ocr or in_doc:
                    fmt = "pdf_ocr" if in_ocr else d["file"].rsplit(".", 1)[-1]
                    facts[(doc_id, alt)] = fmt
    rows = defaultdict(lambda: {"strict": [], "loose": []})
    for (doc_id, alt), fmt in facts.items():
        rows[fmt]["strict"].append(float(strict_key(alt) in strict_key(raw[doc_id])))
        rows[fmt]["loose"].append(float(h.match_key(alt) in h.match_key(raw[doc_id])))
    return {
        fmt: {"n_facts": len(v["strict"]),
              "strict_survival": power.mean(v["strict"]), "strict_se": power.se_mean(v["strict"]),
              "loose_survival": power.mean(v["loose"]), "loose_se": power.se_mean(v["loose"])}
        for fmt, v in sorted(rows.items())
    }


_FACT_RE = re.compile(
    r"0x[0-9A-Fa-f]{4}|[A-Z]{1,3}\d{1,3}(?:\.\d{2})?|[A-Z]{1,3}-\d+|\d+(?:\.\d+)?\s?(?:%RH|Pa|kW|kg|mm|ms|MPa|Hz|A|V|W|s|℃|°C|bps|m³/h|Ω|rpm|小时|分钟)?"
)


def key_facts(answer: str) -> list[str]:
    facts = []
    for m in _FACT_RE.finditer(answer):
        tok = m.group(0).strip()
        if tok and tok not in facts and not re.fullmatch(r"[0-3]", tok):
            facts.append(tok)
    return facts


def build_generation_cases(manifest: dict, parsed: dict, cases: list[dict]) -> list[dict]:
    chunks = h.build_index_chunks(manifest, parsed, "structured")
    keys = [h.match_key(c.content) for c in chunks]
    out = []
    for c in cases:
        if c.get("source") == "template":
            continue
        ctx = []
        for grp in c["evidence"]:
            for i, ch in enumerate(chunks):
                if ch.device_model_id == c["device_model_id"] and ch.is_current and any(
                    h.match_key(a) in keys[i] for a in grp
                ):
                    ctx.append(ch.content)
                    break
        out.append({
            "id": c["id"], "track": "vertical", "category": c["category"], "query": c["query"],
            "device_model_id": c["device_model_id"], "gold_context": ctx,
            "reference_answer": c["answer"],
            "key_facts": key_facts(c["answer"]) if c["answerable"] else [],
            "must_refuse": not c["answerable"],
            "annotation_status": "auto-draft",
        })
    return out


_REFUSAL_RE = re.compile(
    r"无法回答|没有(相关|提及|找到)|未(提及|找到|说明)|手册中(没有|未)|cannot answer|not (mentioned|found|available|specified)|insufficient",
    re.IGNORECASE,
)


def score_generation(gen_cases: list[dict], answers: dict[str, str]) -> dict:
    """Deterministic T4 scoring: key-fact recall, refusal accuracy, false refusals."""
    fact_recall, refusal_ok, false_refusal = [], [], []
    for c in gen_cases:
        ans = answers.get(c["id"], "")
        refused = bool(_REFUSAL_RE.search(ans))
        if c["must_refuse"]:
            refusal_ok.append(float(refused))
        else:
            false_refusal.append(float(refused))
            if c["key_facts"]:
                k = h.match_key(ans)
                fact_recall.append(sum(h.match_key(f) in k for f in c["key_facts"]) / len(c["key_facts"]))
    return {
        "key_fact_recall": power.mean(fact_recall), "key_fact_recall_se": power.se_mean(fact_recall),
        "refusal_accuracy": power.mean(refusal_ok), "false_refusal_rate": power.mean(false_refusal),
        "n_answerable_scored": len(fact_recall), "n_must_refuse": len(refusal_ok),
    }


def write_generation_cases(gen_cases: list[dict]) -> None:
    with GEN_FILE.open("w", encoding="utf-8") as fh:
        for c in gen_cases:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")
