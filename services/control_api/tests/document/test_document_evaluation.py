import hashlib
import math
import re
from pathlib import Path

from app.modules.document.evaluation import (
    RetrievalQuery,
    load_document_dataset,
    run_extraction_evaluation,
    run_retrieval_evaluation,
    score_extraction,
    score_query,
)
from app.modules.document.providers import (
    EmbeddingResult,
    ExtractionResult,
    ProviderError,
)


def _dataset():
    return load_document_dataset(
        Path(__file__).parents[2] / "evals" / "document" / "dataset.json"
    )


def _sourced(value, ref):
    return {"value": value, "confidence": 0.9, "evidence_refs": [ref]}


class OracleExtractionProvider:
    """Returns each group's gold points so the harness itself is checked."""

    def __init__(self, dataset, *, drop=(), extra=()):
        self.dataset = dataset
        self.drop = set(drop)
        self.extra = list(extra)

    async def extract(self, chunks, context):
        document = self.dataset.documents[context["source_document_id"] - 1]
        refs = {chunk.stable_ref for chunk in chunks}
        first_ref = chunks[0].stable_ref
        protocol = document.gold.protocols[0]
        points = [
            {
                "candidate_ref": f"pt{index}",
                "protocol_ref": "proto",
                "code": _sourced(f"p{index}", point.evidence_ref),
                "name": _sourced(point.name, point.evidence_ref),
                "address_text": _sourced(point.address, point.evidence_ref),
                "data_type": _sourced(point.data_type.upper(), point.evidence_ref),
                "access_mode": _sourced(point.access_mode, point.evidence_ref),
                "unit": _sourced(point.unit, point.evidence_ref) if point.unit else None,
            }
            for index, point in enumerate(document.gold.points)
            if point.evidence_ref in refs and point.address not in self.drop
        ]
        for address in self.extra:
            points.append(
                {
                    "candidate_ref": f"extra-{address}",
                    "protocol_ref": "proto",
                    "code": _sourced(address, first_ref),
                    "name": _sourced(address, first_ref),
                    "address_text": _sourced(address, first_ref),
                    "data_type": _sourced("uint16", first_ref),
                    "access_mode": _sourced("R", first_ref),
                }
            )
        first_group = context["group_index"] == 1
        payload = {
            "schema_version": "1.0",
            "device_model": {
                "vendor_name": _sourced("Vendor", first_ref),
                "model_code": _sourced(document.id, first_ref),
                "model_name": _sourced(document.title, first_ref),
            }
            if first_group
            else None,
            "profile": {"version": _sourced("1.0", first_ref)} if first_group else None,
            "protocols": [
                {
                    "candidate_ref": "proto",
                    "code": _sourced(protocol, first_ref),
                    "protocol": _sourced(protocol, first_ref),
                }
            ],
            "points": points,
            "capability_matches": [],
            "point_bindings": [],
            "unresolved_suggestions": [],
        }
        return ExtractionResult(
            payload=payload, raw_response={}, usage={"total_tokens": 10}
        )


def test_dataset_covers_languages_formats_and_traps():
    dataset = _dataset()

    assert {document.language for document in dataset.documents} == {"en", "zh"}
    assert {document.format for document in dataset.documents} == {"pdf", "docx"}
    assert any(
        block.extraction_method == "vision_ocr"
        for document in dataset.documents
        for block in document.blocks
    )
    assert sum(len(document.gold.points) for document in dataset.documents) >= 20
    assert all(document.gold.traps for document in dataset.documents)
    assert len(dataset.queries) >= 30
    tags = {tag for query in dataset.queries for tag in query.tags}
    assert tags >= {"en", "zh", "cross_lingual", "exact_term", "points", "fault"}


async def test_oracle_extraction_scores_perfectly():
    dataset = _dataset()

    report = await run_extraction_evaluation(dataset, OracleExtractionProvider(dataset))

    metrics = report.metrics
    assert metrics.point_precision == 1.0
    assert metrics.point_recall == 1.0
    assert metrics.data_type_accuracy == 1.0
    assert metrics.access_mode_accuracy == 1.0
    assert metrics.unit_accuracy == 1.0
    assert metrics.evidence_accuracy == 1.0
    assert metrics.protocol_accuracy == 1.0
    assert metrics.schema_valid_rate == 1.0
    assert metrics.trap_hit_rate == 0.0
    assert metrics.unknown_evidence_ref_count == 0
    assert metrics.total_tokens == 10 * len(dataset.documents)


async def test_small_groups_still_merge_into_one_candidate():
    dataset = _dataset()

    report = await run_extraction_evaluation(
        dataset, OracleExtractionProvider(dataset), group_max_tokens=50
    )

    assert report.metrics.point_f1 == 1.0
    assert report.metrics.duplicate_point_rate == 0.0


async def test_missed_points_and_traps_lower_the_scores():
    dataset = _dataset()
    provider = OracleExtractionProvider(dataset, drop={"3000", "AI:1"}, extra=["P1.03"])

    report = await run_extraction_evaluation(dataset, provider)

    by_id = {score.document_id: score for score in report.documents}
    assert by_id["acme-pm5300-en"].missed_addresses == ["3000"]
    assert by_id["acme-pm5300-en"].trap_hits == ["P1.03"]
    assert by_id["hx-ahu200-zh"].missed_addresses == ["AI:1"]
    assert report.metrics.point_recall == 20 / 22
    assert report.metrics.trap_hit_rate > 0
    assert report.by_language["en"].point_recall < 1.0


def test_aliases_match_and_wrong_fields_are_counted():
    dataset = _dataset()
    document = next(item for item in dataset.documents if item.id == "hx-ahu200-zh")
    ref = "docx:table:1:row:2"
    payload = {
        "points": [
            {
                "address_text": _sourced("analog-input:1", ref),
                "data_type": _sourced("REAL", ref),
                "access_mode": _sourced("rw", ref),
                "unit": _sourced("°C", ref),
            },
            {"address_text": _sourced("AI 1", "missing-ref")},
        ],
        "protocols": [{"protocol": _sourced("bacnet_ip", ref)}],
    }

    score = score_extraction(
        document, payload, known_refs={chunk.stable_ref for chunk in document.chunks()}
    )

    assert score.matched_points == 1
    assert score.duplicate_points == 1
    assert score.data_type_correct == 0  # REAL is not a supported alias
    assert score.access_mode_correct == 0
    assert score.unit_correct == 1
    assert score.evidence_correct == 1
    assert score.unknown_evidence_refs == 1
    assert score.protocol_correct is True
    assert score.schema_valid is False


async def test_provider_error_counts_every_gold_point_as_missed():
    dataset = _dataset()

    class FailingProvider:
        async def extract(self, chunks, context):
            raise ProviderError("provider_request_rejected")

    report = await run_extraction_evaluation(dataset, FailingProvider())

    assert report.metrics.provider_error_rate == 1.0
    assert report.metrics.point_recall == 0.0
    assert report.documents[0].error == "provider_request_rejected"


class HashingEmbedder:
    """Deterministic bag-of-tokens embedding for ranking tests."""

    dimension = 256

    async def embed(self, texts):
        vectors = []
        for text in texts:
            vector = [0.0] * self.dimension
            tokens = re.findall(r"[a-z0-9]+|[一-鿿]", text.casefold())
            for token in tokens:
                digest = hashlib.sha256(token.encode()).digest()
                vector[int.from_bytes(digest[:4], "big") % self.dimension] += 1.0
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            vectors.append([value / norm for value in vector])
        return EmbeddingResult(
            vectors=vectors,
            provider="test",
            model="hashing",
            dimension=self.dimension,
            usage={},
        )


async def test_retrieval_evaluation_ranks_exact_terms_first():
    dataset = _dataset()

    report = await run_retrieval_evaluation(dataset, HashingEmbedder(), k=5)

    assert report.metrics.queries == len(dataset.queries)
    assert report.metrics.chunks_indexed == sum(
        len(document.chunks()) for document in dataset.documents
    )
    by_id = {result.query_id: result for result in report.queries}
    assert by_id["ahu-av10"].hit_at_1 is True
    assert by_id["boiler-40010"].hit_at_1 is True
    assert 0 < report.metrics.recall_at_k <= 1
    assert set(report.by_tag) >= {"en", "zh", "exact_term"}


def test_score_query_handles_multiple_relevant_refs():
    query = RetrievalQuery(
        id="q", document_id="d", query="x", relevant_refs=["a", "b"], tags=["t"]
    )

    result = score_query(query, ["c", "a", "d", "e", "f", "b"], k=5)

    assert result.first_relevant_rank == 2
    assert result.hit_at_1 is False
    assert result.recall_at_k == 0.5
    assert result.reciprocal_rank == 0.5
