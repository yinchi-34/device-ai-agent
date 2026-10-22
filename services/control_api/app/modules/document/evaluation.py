"""Offline evaluation of profile extraction and manual retrieval.

Both suites run on a frozen corpus of parsed blocks, so they measure the
production chunking, extraction prompts, fragment merging and similarity
ranking without a database or real manuals.
"""

import math
import re
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from time import perf_counter
from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.core.evaluation import percentile, ratio
from app.modules.device.codecs import normalize_data_type
from app.modules.document.agent import merge_candidate_fragments
from app.modules.document.candidate_validation import (
    validate_candidate_evidence,
    validate_candidate_schema,
)
from app.modules.document.chunking import (
    NormalizedChunk,
    build_chunks,
    group_chunks_for_extraction,
)

# Same split-and-repair path that production extraction runs use.
from app.modules.document.extraction import _extract_fragment_with_split
from app.modules.document.parsers import ParsedBlock, ParsedDocument
from app.modules.document.providers import (
    EmbeddingProvider,
    ExtractionModelProvider,
    ProviderError,
)


class EvalBlock(BaseModel):
    content: str
    content_type: str = "text"
    page_number: int | None = None
    extraction_method: str = "pdf_text"
    bounding_box: list[float] | None = None
    confidence: float | None = None
    cells: list[str] | None = None
    table_index: int | None = None
    row_index: int | None = None


class GoldPoint(BaseModel):
    address: str
    aliases: list[str] = Field(default_factory=list)
    name: str
    data_type: str
    access_mode: str
    unit: str | None = None
    evidence_ref: str


class GoldTrap(BaseModel):
    """An address-like string that must never become a point."""

    address: str
    reason: str


class GoldExtraction(BaseModel):
    protocols: list[str] = Field(min_length=1)
    points: list[GoldPoint]
    traps: list[GoldTrap] = Field(default_factory=list)


class EvalDocument(BaseModel):
    id: str
    title: str
    format: str
    media_type: str
    language: str
    blocks: list[EvalBlock] = Field(min_length=1)
    gold: GoldExtraction

    def chunks(self) -> list[NormalizedChunk]:
        return build_chunks(
            ParsedDocument(
                format=self.format,
                blocks=[ParsedBlock(**block.model_dump()) for block in self.blocks],
            )
        )


class RetrievalQuery(BaseModel):
    id: str
    document_id: str
    query: str
    relevant_refs: list[str] = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)


class DocumentEvalDataset(BaseModel):
    version: str
    documents: list[EvalDocument] = Field(min_length=1)
    queries: list[RetrievalQuery] = Field(default_factory=list)

    @model_validator(mode="after")
    def _references_exist(self) -> "DocumentEvalDataset":
        refs = {
            document.id: {chunk.stable_ref for chunk in document.chunks()}
            for document in self.documents
        }
        for document in self.documents:
            for point in document.gold.points:
                if point.evidence_ref not in refs[document.id]:
                    raise ValueError(
                        f"{document.id}: unknown evidence_ref {point.evidence_ref}"
                    )
        for query in self.queries:
            if query.document_id not in refs:
                raise ValueError(f"{query.id}: unknown document {query.document_id}")
            missing = set(query.relevant_refs) - refs[query.document_id]
            if missing:
                raise ValueError(f"{query.id}: unknown refs {sorted(missing)}")
        return self


def load_document_dataset(path: Path) -> DocumentEvalDataset:
    return DocumentEvalDataset.model_validate_json(path.read_text(encoding="utf-8"))


# --- Extraction ---------------------------------------------------------------


class DocumentExtractionScore(BaseModel):
    document_id: str
    language: str
    trial_index: int
    gold_points: int
    predicted_points: int
    matched_points: int
    duplicate_points: int
    missed_addresses: list[str]
    unexpected_addresses: list[str]
    traps: int
    trap_hits: list[str]
    data_type_correct: int
    access_mode_correct: int
    unit_checked: int
    unit_correct: int
    evidence_correct: int
    unknown_evidence_refs: int
    protocol_correct: bool
    schema_valid: bool
    error: str | None = None
    total_tokens: int
    latency_ms: float


class ExtractionMetrics(BaseModel):
    documents: int
    point_precision: float
    point_recall: float
    point_f1: float
    data_type_accuracy: float
    access_mode_accuracy: float
    unit_accuracy: float
    evidence_accuracy: float
    trap_hit_rate: float
    duplicate_point_rate: float
    unknown_evidence_ref_count: int
    protocol_accuracy: float
    schema_valid_rate: float
    provider_error_rate: float
    total_tokens: int
    p95_latency_ms: float


class ExtractionGroupMetrics(BaseModel):
    documents: int
    point_recall: float
    point_f1: float


class ExtractionReport(BaseModel):
    metrics: ExtractionMetrics
    by_language: dict[str, ExtractionGroupMetrics]
    documents: list[DocumentExtractionScore]


def _normalized_address(value: object) -> str:
    return re.sub(r"\s+", "", str(value)).casefold()


def _normalized_unit(value: object) -> str:
    return re.sub(r"\s+", "", str(value)).casefold().replace("°c", "℃")


def _sourced(point: dict[str, Any], field: str) -> str | None:
    value = point.get(field)
    if isinstance(value, dict) and value.get("value") is not None:
        return str(value["value"])
    return None


def _point_refs(point: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    for value in point.values():
        if isinstance(value, dict):
            refs.update(str(ref) for ref in value.get("evidence_refs", []))
    return refs


def _usage_tokens(usage: dict[str, Any]) -> int:
    total = usage.get("total_tokens")
    if isinstance(total, int) and not isinstance(total, bool):
        return total
    return sum(
        value
        for key in ("prompt_tokens", "completion_tokens")
        if isinstance((value := usage.get(key)), int) and not isinstance(value, bool)
    )


def score_extraction(
    document: EvalDocument,
    payload: dict[str, Any],
    *,
    known_refs: set[str],
    trial_index: int = 1,
    error: str | None = None,
    total_tokens: int = 0,
    latency_ms: float = 0.0,
) -> DocumentExtractionScore:
    gold = document.gold
    gold_by_address: dict[str, int] = {}
    for index, point in enumerate(gold.points):
        for address in (point.address, *point.aliases):
            gold_by_address[_normalized_address(address)] = index
    traps = {_normalized_address(trap.address): trap.address for trap in gold.traps}

    matched: dict[int, dict[str, Any]] = {}
    duplicates = 0
    unexpected: list[str] = []
    trap_hits: list[str] = []
    predicted = [point for point in payload.get("points", []) if isinstance(point, dict)]
    for point in predicted:
        address = _normalized_address(_sourced(point, "address_text") or "")
        if address in traps:
            trap_hits.append(traps[address])
        index = gold_by_address.get(address)
        if index is None:
            unexpected.append(_sourced(point, "address_text") or "<none>")
        elif index in matched:
            duplicates += 1
        else:
            matched[index] = point

    data_type_correct = access_mode_correct = 0
    unit_checked = unit_correct = evidence_correct = 0
    for index, point in matched.items():
        expected = gold.points[index]
        data_type_correct += normalize_data_type(
            _sourced(point, "data_type")
        ) == normalize_data_type(expected.data_type)
        access_mode_correct += (_sourced(point, "access_mode") or "").upper() == (
            expected.access_mode.upper()
        )
        if expected.unit:
            unit_checked += 1
            unit_correct += _normalized_unit(expected.unit) in _normalized_unit(
                _sourced(point, "unit") or ""
            )
        evidence_correct += expected.evidence_ref in _point_refs(point)

    predicted_protocols = {
        (_sourced(protocol, "protocol") or "").casefold()
        for protocol in payload.get("protocols", [])
        if isinstance(protocol, dict)
    }
    return DocumentExtractionScore(
        document_id=document.id,
        language=document.language,
        trial_index=trial_index,
        gold_points=len(gold.points),
        predicted_points=len(predicted),
        matched_points=len(matched),
        duplicate_points=duplicates,
        missed_addresses=[
            point.address
            for index, point in enumerate(gold.points)
            if index not in matched
        ],
        unexpected_addresses=unexpected,
        traps=len(traps),
        trap_hits=trap_hits,
        data_type_correct=data_type_correct,
        access_mode_correct=access_mode_correct,
        unit_checked=unit_checked,
        unit_correct=unit_correct,
        evidence_correct=evidence_correct,
        unknown_evidence_refs=len(validate_candidate_evidence(payload, known_refs)),
        protocol_correct=predicted_protocols == set(gold.protocols),
        schema_valid=error is None and not validate_candidate_schema(payload),
        error=error,
        total_tokens=total_tokens,
        latency_ms=latency_ms,
    )


def _f1(precision: float, recall: float) -> float:
    return ratio(2 * precision * recall, precision + recall)


def build_extraction_report(
    scores: Sequence[DocumentExtractionScore],
) -> ExtractionReport:
    if not scores:
        raise ValueError("no extraction scores")

    def totals(items: Sequence[DocumentExtractionScore]) -> tuple[float, float]:
        matched = sum(item.matched_points for item in items)
        precision = ratio(matched, sum(item.predicted_points for item in items))
        recall = ratio(matched, sum(item.gold_points for item in items))
        return precision, recall

    precision, recall = totals(scores)
    matched = sum(score.matched_points for score in scores)
    by_language: dict[str, list[DocumentExtractionScore]] = defaultdict(list)
    for score in scores:
        by_language[score.language].append(score)
    return ExtractionReport(
        metrics=ExtractionMetrics(
            documents=len(scores),
            point_precision=precision,
            point_recall=recall,
            point_f1=_f1(precision, recall),
            data_type_accuracy=ratio(
                sum(score.data_type_correct for score in scores), matched
            ),
            access_mode_accuracy=ratio(
                sum(score.access_mode_correct for score in scores), matched
            ),
            unit_accuracy=ratio(
                sum(score.unit_correct for score in scores),
                sum(score.unit_checked for score in scores),
            ),
            evidence_accuracy=ratio(
                sum(score.evidence_correct for score in scores), matched
            ),
            trap_hit_rate=ratio(
                sum(len(set(score.trap_hits)) for score in scores),
                sum(score.traps for score in scores),
            ),
            duplicate_point_rate=ratio(
                sum(score.duplicate_points for score in scores),
                sum(score.predicted_points for score in scores),
            ),
            unknown_evidence_ref_count=sum(
                score.unknown_evidence_refs for score in scores
            ),
            protocol_accuracy=ratio(
                sum(score.protocol_correct for score in scores), len(scores)
            ),
            schema_valid_rate=ratio(
                sum(score.schema_valid for score in scores), len(scores)
            ),
            provider_error_rate=ratio(
                sum(score.error is not None for score in scores), len(scores)
            ),
            total_tokens=sum(score.total_tokens for score in scores),
            p95_latency_ms=percentile([score.latency_ms for score in scores], 0.95),
        ),
        by_language={
            language: ExtractionGroupMetrics(
                documents=len(items),
                point_recall=totals(items)[1],
                point_f1=_f1(*totals(items)),
            )
            for language, items in sorted(by_language.items())
        },
        documents=list(scores),
    )


async def extract_document(
    provider: ExtractionModelProvider,
    document: EvalDocument,
    chunks: list[NormalizedChunk],
    *,
    document_index: int,
    group_max_tokens: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run grouped fragment extraction and merging as production runs do."""
    groups = group_chunks_for_extraction(chunks, max_tokens=group_max_tokens)
    payloads: list[dict[str, Any]] = []
    usage: dict[str, Any] = {}
    for group_index, group in enumerate(groups, start=1):
        result = await _extract_fragment_with_split(
            provider,
            group,
            {
                "source_document_id": document_index,
                "media_type": document.media_type,
                "extraction_mode": "fragment",
                "group_index": group_index,
                "group_count": len(groups),
            },
        )
        payloads.append(result.payload)
        for key, value in result.usage.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                usage[key] = usage.get(key, 0) + value
    return merge_candidate_fragments(payloads), usage


async def run_extraction_evaluation(
    dataset: DocumentEvalDataset,
    provider: ExtractionModelProvider,
    *,
    group_max_tokens: int = 2000,
    trials: int = 1,
) -> ExtractionReport:
    if trials < 1:
        raise ValueError("trials must be positive")
    scores: list[DocumentExtractionScore] = []
    for document_index, document in enumerate(dataset.documents, start=1):
        chunks = document.chunks()
        known_refs = {chunk.stable_ref for chunk in chunks}
        for trial_index in range(1, trials + 1):
            started = perf_counter()
            try:
                payload, usage = await extract_document(
                    provider,
                    document,
                    chunks,
                    document_index=document_index,
                    group_max_tokens=group_max_tokens,
                )
            except ProviderError as exc:
                payload, usage, error = {}, {}, exc.code
            else:
                error = None
            scores.append(
                score_extraction(
                    document,
                    payload,
                    known_refs=known_refs,
                    trial_index=trial_index,
                    error=error,
                    total_tokens=_usage_tokens(usage),
                    latency_ms=(perf_counter() - started) * 1000,
                )
            )
    return build_extraction_report(scores)


# --- Retrieval ----------------------------------------------------------------


class QueryResult(BaseModel):
    query_id: str
    document_id: str
    tags: list[str]
    top_refs: list[str]
    first_relevant_rank: int | None
    hit_at_1: bool
    recall_at_k: float
    reciprocal_rank: float


class RetrievalMetrics(BaseModel):
    queries: int
    k: int
    hit_at_1: float
    recall_at_k: float
    mrr_at_10: float
    chunks_indexed: int
    embedding_seconds: float


class RetrievalGroupMetrics(BaseModel):
    queries: int
    hit_at_1: float
    recall_at_k: float
    mrr_at_10: float


class RetrievalReport(BaseModel):
    metrics: RetrievalMetrics
    by_tag: dict[str, RetrievalGroupMetrics]
    queries: list[QueryResult]


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


def rank_chunks(
    query_vector: Sequence[float],
    chunks: Sequence[NormalizedChunk],
    vectors: Sequence[Sequence[float]],
) -> list[NormalizedChunk]:
    """Order like ``search_document_chunks``: cosine distance, then chunk order."""
    scored = [
        (1.0 - _cosine_similarity(query_vector, vector), chunk.block_index, chunk)
        for chunk, vector in zip(chunks, vectors, strict=True)
    ]
    scored.sort(key=lambda item: (item[0], item[1]))
    return [chunk for _, _, chunk in scored]


def score_query(
    query: RetrievalQuery, ranked_refs: Sequence[str], *, k: int
) -> QueryResult:
    relevant = set(query.relevant_refs)
    first_rank = next(
        (rank for rank, ref in enumerate(ranked_refs, start=1) if ref in relevant),
        None,
    )
    return QueryResult(
        query_id=query.id,
        document_id=query.document_id,
        tags=query.tags,
        top_refs=list(ranked_refs[:k]),
        first_relevant_rank=first_rank,
        hit_at_1=first_rank == 1,
        recall_at_k=len(relevant & set(ranked_refs[:k])) / len(relevant),
        reciprocal_rank=1 / first_rank if first_rank and first_rank <= 10 else 0.0,
    )


async def _embed_all(
    provider: EmbeddingProvider, texts: Sequence[str], batch_size: int
) -> list[list[float]]:
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = list(texts[start : start + batch_size])
        result = await provider.embed(batch)
        if len(result.vectors) != len(batch):
            raise ProviderError("provider_embedding_count_mismatch")
        vectors.extend(result.vectors)
    return vectors


def build_retrieval_report(
    results: Sequence[QueryResult],
    *,
    k: int,
    chunks_indexed: int,
    embedding_seconds: float,
) -> RetrievalReport:
    if not results:
        raise ValueError("no retrieval queries")

    def group(items: Sequence[QueryResult]) -> RetrievalGroupMetrics:
        return RetrievalGroupMetrics(
            queries=len(items),
            hit_at_1=ratio(sum(item.hit_at_1 for item in items), len(items)),
            recall_at_k=ratio(sum(item.recall_at_k for item in items), len(items)),
            mrr_at_10=ratio(sum(item.reciprocal_rank for item in items), len(items)),
        )

    overall = group(results)
    by_tag: dict[str, list[QueryResult]] = defaultdict(list)
    for result in results:
        for tag in result.tags:
            by_tag[tag].append(result)
    return RetrievalReport(
        metrics=RetrievalMetrics(
            queries=overall.queries,
            k=k,
            hit_at_1=overall.hit_at_1,
            recall_at_k=overall.recall_at_k,
            mrr_at_10=overall.mrr_at_10,
            chunks_indexed=chunks_indexed,
            embedding_seconds=embedding_seconds,
        ),
        by_tag={tag: group(items) for tag, items in sorted(by_tag.items())},
        queries=list(results),
    )


async def run_retrieval_evaluation(
    dataset: DocumentEvalDataset,
    provider: EmbeddingProvider,
    *,
    k: int = 5,
    batch_size: int = 64,
) -> RetrievalReport:
    """Score ``search_manual`` ranking; each query is scoped to its document."""
    if k < 1:
        raise ValueError("k must be positive")
    started = perf_counter()
    indexed: dict[str, tuple[list[NormalizedChunk], list[list[float]]]] = {}
    for document in dataset.documents:
        chunks = document.chunks()
        vectors = await _embed_all(
            provider, [chunk.content for chunk in chunks], batch_size
        )
        indexed[document.id] = (chunks, vectors)
    query_vectors = await _embed_all(
        provider, [query.query for query in dataset.queries], batch_size
    )
    embedding_seconds = perf_counter() - started

    results = []
    for query, query_vector in zip(dataset.queries, query_vectors, strict=True):
        chunks, vectors = indexed[query.document_id]
        ranked = rank_chunks(query_vector, chunks, vectors)
        results.append(
            score_query(query, [chunk.stable_ref for chunk in ranked], k=k)
        )
    return build_retrieval_report(
        results,
        k=k,
        chunks_indexed=sum(len(chunks) for chunks, _ in indexed.values()),
        embedding_seconds=embedding_seconds,
    )
