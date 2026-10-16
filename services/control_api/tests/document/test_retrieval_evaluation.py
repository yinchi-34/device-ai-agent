import math
from pathlib import Path

import pytest

from app.modules.document.chunking import build_chunks
from app.modules.document.parsers import parse_docx, parse_pdf
from app.modules.document.retrieval import DocumentSearchResult
from app.modules.document.retrieval_evaluation import (
    HashedNgramEmbeddingProvider,
    RetrievalCase,
    RetrievalDataset,
    aggregate,
    format_report,
    load_retrieval_dataset,
    matches_evidence,
    run_retrieval_evaluation,
    score_case,
)

DATASET_PATH = (
    Path(__file__).resolve().parents[2] / "evals" / "retrieval" / "cases.json"
)


class _NoOcr:
    def render_png(self, pdf_path, page_number):
        raise AssertionError(f"{pdf_path.name} page {page_number} needs OCR")

    async def extract_page(self, image_bytes, page_number):
        raise AssertionError("fixtures must not need OCR")


def _result(chunk_id, content, *, document_id=1, similarity=0.9):
    return DocumentSearchResult(
        chunk_id=chunk_id,
        source_document_id=document_id,
        stable_ref=f"ref:{chunk_id}",
        page_number=1,
        content=content,
        similarity=similarity,
    )


def test_matches_evidence_ignores_whitespace_and_requires_all_terms():
    evidence = [["A05", "过滤器压差高"], ["30011"]]

    assert matches_evidence("A05 过滤器\n压差 高", evidence) == [0]
    assert matches_evidence("A05 送风静压低", evidence) == []
    assert matches_evidence("a05 过滤器压差高 30011", evidence) == [0, 1]


def test_score_case_ranks_duplicates_and_scope():
    case = RetrievalCase(
        id="c",
        device_model="ahu",
        category="exact_code",
        query="A05",
        evidence=[["A05"], ["30011"]],
    )
    results = [
        _result(1, "unrelated page"),
        _result(2, "A05 过滤器压差高"),
        _result(3, "A05  过滤器压差高"),
        _result(4, "30011 过滤器压差", document_id=2),
        _result(5, "30011 beyond k"),
    ]

    scored = score_case(
        case,
        results,
        k=4,
        document_model={1: "ahu", 2: "other"},
        latency_ms=3.0,
    )

    assert scored.first_relevant_rank == 2
    assert (scored.hit_at_1, scored.hit_at_3, scored.hit_at_k) == (False, True, True)
    assert scored.reciprocal_rank == 0.5
    assert scored.evidence_recall_at_k == 1.0
    assert scored.duplicates_at_k == 1
    assert scored.out_of_scope_at_k == 1
    assert scored.context_chars_at_k == sum(
        len(result.content) for result in results[:4]
    )


def test_aggregate_handles_misses():
    case = RetrievalCase(
        id="c", device_model="m", category="spec", query="q", evidence=[["x"]]
    )
    missed = score_case(
        case, [_result(1, "nothing")], k=5, document_model={1: "m"}, latency_ms=1
    )

    metrics = aggregate([missed], k=5)

    assert metrics.hit_at_k == 0.0
    assert metrics.mrr == 0.0
    assert metrics.duplicate_rate_at_k == 0.0


async def test_offline_embedding_is_deterministic_and_normalised():
    provider = HashedNgramEmbeddingProvider(dimension=256)

    first = await provider.embed(["A05 过滤器压差高", "完全无关的内容"])
    second = await provider.embed(["A05 过滤器压差高"])

    assert first.vectors[0] == second.vectors[0]
    assert first.dimension == 256
    for vector in first.vectors:
        assert math.isclose(sum(value * value for value in vector), 1.0)

    query = (await provider.embed(["过滤器压差"])).vectors[0]

    def cosine(left, right):
        return sum(a * b for a, b in zip(left, right, strict=True))

    assert cosine(query, first.vectors[0]) > cosine(query, first.vectors[1])


def test_dataset_rejects_unknown_references():
    with pytest.raises(ValueError):
        RetrievalDataset.model_validate(
            {
                "version": "1",
                "device_models": [],
                "documents": [],
                "cases": [
                    {
                        "id": "c",
                        "device_model": "missing",
                        "category": "spec",
                        "query": "q",
                        "evidence": [["x"]],
                    }
                ],
            }
        )


async def test_every_label_is_present_in_its_manual():
    """Guards against label rot when fixtures are regenerated."""
    dataset = load_retrieval_dataset(DATASET_PATH)
    text_by_model: dict[str, str] = {}
    for document in dataset.documents:
        path = DATASET_PATH.parent / document.path
        if path.suffix == ".pdf":
            parsed = await parse_pdf(
                path, renderer=_NoOcr(), vision=_NoOcr(), min_text_chars=40
            )
        else:
            parsed = parse_docx(path)
        text = "".join(chunk.content for chunk in build_chunks(parsed))
        text_by_model[document.device_model] = (
            text_by_model.get(document.device_model, "") + text
        )

    for case in dataset.cases:
        manual = text_by_model[case.device_model]
        assert matches_evidence(manual, case.evidence) == list(
            range(len(case.evidence))
        ), f"{case.id}: evidence not found in its manual"


async def test_full_evaluation_runs_on_database(db_session):
    dataset = load_retrieval_dataset(DATASET_PATH)

    report = await run_retrieval_evaluation(
        db_session,
        dataset,
        base_dir=DATASET_PATH.parent,
        provider=HashedNgramEmbeddingProvider(),
        embedding_model="hashed-ngram-v1",
    )

    assert [scenario.name for scenario in report.scenarios] == [
        "single_run",
        "after_retry",
    ]
    single_run = report.scenarios[0]
    assert single_run.metrics.cases == len(dataset.cases)
    assert single_run.metrics.duplicate_rate_at_k == 0.0
    for scenario in report.scenarios:
        assert scenario.metrics.out_of_scope_results == 0
    assert set(report.corpus) == {document.key for document in dataset.documents}
    assert "single_run" in format_report(report)
