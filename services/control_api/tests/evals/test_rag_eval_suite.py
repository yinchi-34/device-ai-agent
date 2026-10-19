import math

import pytest

from evals.rag import harness as h
from evals.rag import metrics as m

# --- metrics -----------------------------------------------------------------

RANKS = [set(), {0}, {0}, {1}, set()]  # evidence groups covered per rank


def test_hit_and_recall_use_union_of_top_k():
    assert m.hit_at_k(RANKS, 1) == 0.0
    assert m.hit_at_k(RANKS, 2) == 1.0
    assert m.recall_at_k(RANKS, 2, 3) == 0.5
    assert m.recall_at_k(RANKS, 2, 4) == 1.0
    assert m.full_coverage_at_k(RANKS, 2, 3) == 0.0
    assert m.full_coverage_at_k(RANKS, 2, 4) == 1.0


def test_precision_divides_by_k():
    assert m.precision_at_k(RANKS, 5) == pytest.approx(3 / 5)


def test_reciprocal_rank():
    assert m.reciprocal_rank(RANKS, 10) == 0.5
    assert m.reciprocal_rank([set(), set()], 10) == 0.0


def test_ndcg_gives_no_gain_to_redundant_chunks():
    ideal = [{0}, {1}]
    assert m.ndcg_at_k(ideal, 2, 10) == pytest.approx(1.0)
    redundant = [{0}, {0}, {1}]
    expected = (1 + 1 / math.log2(4)) / (1 + 1 / math.log2(3))
    assert m.ndcg_at_k(redundant, 2, 10) == pytest.approx(expected)


def test_average_precision_counts_only_novel_hits():
    assert m.average_precision_at_k([{0}, {1}], 2, 10) == pytest.approx(1.0)
    assert m.average_precision_at_k([set(), {0}], 1, 10) == pytest.approx(0.5)


def test_duplicate_rate_and_auroc():
    assert m.duplicate_rate(["a", "b", "a", "a"]) == 0.5
    assert m.auroc([0.9, 0.8], [0.1, 0.2]) == 1.0
    assert m.auroc([0.5], [0.5]) == 0.5


def test_randomization_test_detects_identical_systems():
    a = [0.1, 0.5, 0.9, 0.3]
    assert m.paired_randomization_test(a, a, trials=200) == pytest.approx(1.0)


def test_character_error_rate():
    assert m.character_error_rate("75 ohrn", "75 ohm") == pytest.approx(2 / 6)


# --- normalisation / tokenisation -------------------------------------------


def test_fold_text_repairs_pdf_radical_codepoints():
    extracted = "⻛机频率 ⽤⼾⼿册 Speciﬁcations 12.0〜30.0"
    folded = h.fold_text(extracted)
    assert "风机频率" in folded
    assert "Specifications" in folded
    assert "12.0~30.0" in folded


def test_tokenizer_keeps_codes_whole_and_bigrams_cjk():
    tokens = h.tokenize("P0.05 0x2000 40013 送风温度")
    assert {"p0.05", "0x2000", "40013", "送风", "风温", "温度"} <= set(tokens)


def test_truncate_to_budget_respects_budget():
    text = h._truncate_to_budget(["a" * 400, "b" * 4000], 150)
    assert h.estimate_tokens(text) <= 151


# --- dataset integrity and harness invariants -------------------------------


@pytest.fixture(scope="module")
def corpus():
    manifest = h.load_manifest()
    return manifest, h.parse_corpus(manifest), h.load_cases()


def test_gold_evidence_exists_in_scope(corpus):
    manifest, _, cases = corpus
    assert h.validate_gold(manifest, cases) == []


def test_case_ids_unique_and_categories_balanced(corpus):
    _, _, cases = corpus
    assert len({c["id"] for c in cases}) == len(cases)
    by_cat = {}
    for c in cases:
        by_cat[c["category"]] = by_cat.get(c["category"], 0) + 1
    assert min(by_cat.values()) >= 4
    assert sum(not c["answerable"] for c in cases) >= 10


def test_scanned_page_goes_through_ocr(corpus):
    _, parsed, _ = corpus
    assert parsed["vfd750"].metadata["ocr_pages"]


def test_scoped_retrieval_never_leaves_scope(corpus):
    manifest, parsed, cases = corpus
    report = h.evaluate_run(h.RunConfig("t", "structured"), manifest, parsed, cases)
    doc_model = {d["doc_id"]: d["device_model_id"] for d in manifest["documents"]}
    case_model = {c["id"]: c["device_model_id"] for c in cases}
    for row in report["per_query"]:
        for ref in row["top_refs"]:
            assert doc_model[ref.split(":", 1)[0]] == case_model[row["id"]]


def test_dedupe_removes_retry_duplicates(corpus):
    manifest, parsed, cases = corpus
    dup = h.evaluate_run(h.RunConfig("d", "baseline", retry_runs=2), manifest, parsed, cases)
    dedup = h.evaluate_run(h.RunConfig("dd", "baseline", retry_runs=2, dedupe=True), manifest, parsed, cases)
    assert dup["summary"]["overall"]["dup_rate@5"] > 0.2
    assert dedup["summary"]["overall"]["dup_rate@5"] == 0.0
