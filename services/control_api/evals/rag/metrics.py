"""Retrieval metrics for the RAG evaluation suite.

All functions are pure and operate on per-query relevance information so they
can be unit-tested without a corpus. References for each metric are listed in
``evals/rag/README.md``.

Terminology
-----------
* ``groups_per_rank``: for each retrieved chunk (rank 1..n) the set of evidence
  group indices it covers.
* ``n_groups``: number of evidence groups the query needs.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence


def hit_at_k(groups_per_rank: Sequence[set[int]], k: int) -> float:
    """1 if any of the top-k chunks covers any evidence group (a.k.a. Hit Rate / Success@k)."""
    return float(any(groups_per_rank[:k]))


def recall_at_k(groups_per_rank: Sequence[set[int]], n_groups: int, k: int) -> float:
    """Fraction of evidence groups covered by the union of the top-k chunks.

    Evidence-level recall: chunker-agnostic, so different chunking strategies
    are comparable (cf. Chroma's token-level recall and RAGChecker claim recall).
    """
    if n_groups == 0:
        return 0.0
    covered: set[int] = set()
    for groups in groups_per_rank[:k]:
        covered |= groups
    return len(covered) / n_groups


def full_coverage_at_k(groups_per_rank: Sequence[set[int]], n_groups: int, k: int) -> float:
    """1 only if ALL evidence groups are covered in the top-k (strict multi-hop success)."""
    return float(n_groups > 0 and recall_at_k(groups_per_rank, n_groups, k) == 1.0)


def precision_at_k(groups_per_rank: Sequence[set[int]], k: int) -> float:
    """Share of the top-k slots holding a relevant chunk (divides by k, as in TREC P@k)."""
    if k <= 0:
        return 0.0
    return sum(1 for groups in groups_per_rank[:k] if groups) / k


def reciprocal_rank(groups_per_rank: Sequence[set[int]], k: int) -> float:
    """1 / rank of the first relevant chunk within the top-k, else 0 (MRR when averaged)."""
    for rank, groups in enumerate(groups_per_rank[:k], start=1):
        if groups:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(groups_per_rank: Sequence[set[int]], n_groups: int, k: int) -> float:
    """nDCG@k with *novel-evidence* gain.

    A chunk's gain is the number of evidence groups it covers that no earlier
    chunk covered, so redundant duplicates earn nothing. The ideal ranking
    places one chunk per group first, i.e. IDCG = sum_{i=1..min(n,k)} 1/log2(i+1).
    """
    if n_groups == 0:
        return 0.0
    seen: set[int] = set()
    dcg = 0.0
    for rank, groups in enumerate(groups_per_rank[:k], start=1):
        novel = groups - seen
        seen |= groups
        if novel:
            dcg += len(novel) / math.log2(rank + 1)
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(n_groups, k) + 1))
    return dcg / ideal if ideal else 0.0


def average_precision_at_k(groups_per_rank: Sequence[set[int]], n_groups: int, k: int) -> float:
    """AP@k where a rank counts as a relevant hit only when it adds a new evidence group."""
    if n_groups == 0:
        return 0.0
    seen: set[int] = set()
    hits = 0
    total = 0.0
    for rank, groups in enumerate(groups_per_rank[:k], start=1):
        novel = groups - seen
        if novel:
            seen |= groups
            hits += 1
            total += hits / rank
    return total / min(n_groups, k)


def context_efficiency(evidence_chars: int, retrieved_chars: int) -> float:
    """Evidence characters / retrieved characters in the top-k (token-level precision proxy)."""
    if retrieved_chars <= 0:
        return 0.0
    return min(1.0, evidence_chars / retrieved_chars)


def duplicate_rate(hashes: Sequence[str]) -> float:
    """Share of top-k chunks whose content duplicates a higher-ranked chunk."""
    if not hashes:
        return 0.0
    seen: set[str] = set()
    dup = 0
    for h in hashes:
        if h in seen:
            dup += 1
        seen.add(h)
    return dup / len(hashes)


def auroc(positive_scores: Sequence[float], negative_scores: Sequence[float]) -> float:
    """Area under the ROC curve via the Mann-Whitney U statistic (ties count 0.5).

    Used to ask: does the top-1 retrieval score separate answerable from
    unanswerable queries? 0.5 = no separation, 1.0 = perfect.
    """
    if not positive_scores or not negative_scores:
        return float("nan")
    wins = 0.0
    for p in positive_scores:
        for n in negative_scores:
            if p > n:
                wins += 1.0
            elif p == n:
                wins += 0.5
    return wins / (len(positive_scores) * len(negative_scores))


def paired_randomization_test(
    a: Sequence[float], b: Sequence[float], *, trials: int = 10000, seed: int = 13
) -> float:
    """Two-sided paired randomization (permutation) test p-value for mean(a) - mean(b).

    Recommended for IR system comparison by Smucker, Allan & Carterette (CIKM 2007).
    """
    if len(a) != len(b):
        raise ValueError("paired samples must have equal length")
    n = len(a)
    if n == 0:
        return float("nan")
    diffs = [x - y for x, y in zip(a, b, strict=True)]
    observed = abs(sum(diffs) / n)
    rng = random.Random(seed)
    extreme = 0
    for _ in range(trials):
        s = sum(d if rng.random() < 0.5 else -d for d in diffs)
        if abs(s / n) >= observed - 1e-12:
            extreme += 1
    return (extreme + 1) / (trials + 1)


def levenshtein(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def character_error_rate(hypothesis: str, reference: str) -> float:
    """CER = edit distance / reference length (standard OCR metric)."""
    if not reference:
        return 0.0 if not hypothesis else 1.0
    return levenshtein(hypothesis, reference) / len(reference)
