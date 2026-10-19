"""General-domain (通用) retrieval track built from CRUD-RAG.

CRUD-RAG (Lyu et al., arXiv:2401.17043, https://github.com/IAAR-Shanghai/CRUD_RAG)
is a Chinese news benchmark whose question-answering subsets come in 1-, 2- and
3-document variants with the gold news passages attached — a natural hop-count
difficulty axis that mirrors HotpotQA / MultiHop-RAG.

The dataset is NOT vendored into this repository (no licence file in the
upstream repo); it is downloaded on first use into ``evals/rag/.cache``.

Corpus  = every unique news passage that appears in the three QA subsets
          (passages about the same event act as hard negatives).
Qrels   = the passages attached to each question; ALL are needed (evidence
          groups), so the vertical-track metrics apply unchanged.
Caveat  = other passages about the same event may also answer a 1-doc question
          and are judged non-relevant (an unjudged "hole", cf. BEIR §5): audit a
          sample by hand before drawing absolute conclusions.
"""

from __future__ import annotations

import hashlib
import json
import random
import urllib.request
from pathlib import Path

from evals.rag import harness as h
from evals.rag import metrics as m

CRUD_URL = (
    "https://raw.githubusercontent.com/IAAR-Shanghai/CRUD_RAG/main/data/crud_split/split_merged.json"
)
CACHE = h.RAG_DIR / ".cache" / "crud_rag_split_merged.json"
SUBSETS = {"questanswer_1doc": 1, "questanswer_2docs": 2, "questanswer_3docs": 3}


def load_crud(path: Path | None = None) -> dict:
    path = path or CACHE
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(CRUD_URL, timeout=300) as resp:
            path.write_bytes(resp.read())
    return json.loads(path.read_text(encoding="utf-8"))


def _pid(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]


def build_general_track(data: dict, *, per_level: int = 200, seed: int = 7) -> tuple[dict[str, str], list[dict]]:
    """Return (passages by id, cases). Cases are a stratified sample per hop level."""
    passages: dict[str, str] = {}
    pools: dict[int, list[dict]] = {1: [], 2: [], 3: []}
    for subset, hops in SUBSETS.items():
        for item in data[subset]:
            news = [item[f"news{i}"] for i in range(1, hops + 1)]
            ids = []
            for text in news:
                pid = _pid(text)
                passages[pid] = text.strip()
                ids.append(pid)
            pools[hops].append(
                {
                    "id": f"crud-{hops}doc-{item['ID']}",
                    "track": "general",
                    "source": "crud-rag",
                    "category": f"news_{hops}doc",
                    "query": item["questions"].strip(),
                    "gold_passages": list(dict.fromkeys(ids)),
                    "answer": item["answers"].strip(),
                    "answerable": True,
                }
            )
    rng = random.Random(seed)
    cases = []
    for hops in (1, 2, 3):
        pool = sorted(pools[hops], key=lambda c: c["id"])
        cases.extend(rng.sample(pool, min(per_level, len(pool))))
    return passages, cases


def evaluate_general(passages: dict[str, str], cases: list[dict], retrievers: dict) -> dict:
    """Doc-id qrels; retrievers maps name -> callable(query) -> ranked passage ids."""
    out = {}
    for name, rank in retrievers.items():
        rows = []
        for c in cases:
            gold = c["gold_passages"]
            top = rank(c["query"])[: max(h.KS)]
            gpr = [{g for g, pid in enumerate(gold) if pid == t} for t in top]
            n = len(gold)
            row = {"id": c["id"], "category": c["category"], "answerable": True}
            for k in h.KS:
                row[f"hit@{k}"] = m.hit_at_k(gpr, k)
                row[f"recall@{k}"] = m.recall_at_k(gpr, n, k)
                row[f"full@{k}"] = m.full_coverage_at_k(gpr, n, k)
            row["mrr@10"] = m.reciprocal_rank(gpr, 10)
            row["ndcg@10"] = m.ndcg_at_k(gpr, n, 10)
            rows.append(row)
        out[name] = rows
    return out


def bm25_ranker(passages: dict[str, str], tokenizer=None):
    ids = list(passages)
    index = h.BM25([passages[i] for i in ids], tokenizer=tokenizer)
    everything = range(len(ids))

    def rank(query: str) -> list[str]:
        sc = index.scores(query, everything)
        best = sorted((i for i in everything if sc[i] > 0), key=lambda i: (-sc[i], i))[: max(h.KS)]
        return [ids[i] for i in best]

    return rank
