"""Offline evaluation of manual retrieval (the RAG path used by diagnosis).

The harness runs the production path end to end: the real parsers and
``build_chunks`` turn fixture manuals into chunks, ``embed_run_chunks`` embeds
them, and ``search_document_chunks`` answers each case scoped to a device
model, exactly as the diagnosis agent's ``search_manual`` tool does.

Relevance is judged by content, not by chunk identity: a chunk is relevant to
a case when it contains every term of at least one evidence item, ignoring
whitespace. Labels therefore stay valid when chunking changes.
"""

import json
import math
import re
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from hashlib import blake2b, sha256
from pathlib import Path
from statistics import mean
from typing import Any

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.device.models import DeviceModel
from app.modules.document.chunking import build_chunks
from app.modules.document.enums import ExtractionRunStatus
from app.modules.document.models import (
    DocumentChunk,
    ProfileExtractionRun,
    SourceDocument,
)
from app.modules.document.parsers import parse_docx, parse_pdf
from app.modules.document.providers import EmbeddingProvider, EmbeddingResult
from app.modules.document.retrieval import (
    DocumentSearchResult,
    embed_run_chunks,
    search_document_chunks,
)

DEFAULT_K = 5
SEARCH_DEPTH = 10
_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
}


class RetrievalDeviceModel(BaseModel):
    key: str
    vendor_name: str
    model_code: str
    model_name: str


class RetrievalDocument(BaseModel):
    key: str
    device_model: str
    path: str


class RetrievalCase(BaseModel):
    id: str
    device_model: str
    category: str
    query: str = Field(min_length=1)
    evidence: list[list[str]] = Field(min_length=1)

    @model_validator(mode="after")
    def _evidence_terms_present(self) -> "RetrievalCase":
        for item in self.evidence:
            if not item or any(not normalize_text(term) for term in item):
                raise ValueError(f"case {self.id} has an empty evidence item")
        return self


class RetrievalDataset(BaseModel):
    version: str
    description: str = ""
    device_models: list[RetrievalDeviceModel]
    documents: list[RetrievalDocument]
    cases: list[RetrievalCase]

    @model_validator(mode="after")
    def _references_resolve(self) -> "RetrievalDataset":
        model_keys = {model.key for model in self.device_models}
        for document in self.documents:
            if document.device_model not in model_keys:
                raise ValueError(f"document {document.key} has unknown device_model")
        case_ids = [case.id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("case ids must be unique")
        for case in self.cases:
            if case.device_model not in model_keys:
                raise ValueError(f"case {case.id} has unknown device_model")
        return self


def load_retrieval_dataset(path: Path) -> RetrievalDataset:
    return RetrievalDataset.model_validate(json.loads(path.read_text("utf-8")))


def normalize_text(text: str) -> str:
    return "".join(text.split()).casefold()


def matches_evidence(content: str, evidence: Sequence[Sequence[str]]) -> list[int]:
    """Return the indexes of evidence items fully contained in ``content``."""
    normalized = normalize_text(content)
    return [
        index
        for index, item in enumerate(evidence)
        if all(normalize_text(term) in normalized for term in item)
    ]


# --- Offline embedding -------------------------------------------------------

_ASCII_TOKEN = re.compile(r"[a-z0-9]+(?:\.[a-z0-9]+)*")
_CJK = re.compile(r"[㐀-鿿]+")


def _features(text: str) -> Iterable[str]:
    lowered = text.casefold()
    for token in _ASCII_TOKEN.findall(lowered):
        yield f"w:{token}"
    for run in _CJK.findall(lowered):
        for index, char in enumerate(run):
            yield f"c:{char}"
            if index + 1 < len(run):
                yield f"b:{run[index:index + 2]}"


@dataclass
class HashedNgramEmbeddingProvider:
    """Deterministic, network-free embedding for CI and offline baselines.

    Hashes ASCII words plus CJK unigrams and bigrams into a fixed-size,
    L2-normalised vector with sublinear term frequency. It approximates a
    lexical retriever, so it is a regression baseline, not a stand-in for the
    quality of a neural embedding model.
    """

    dimension: int = 1024
    provider: str = "offline"
    model: str = "hashed-ngram-v1"

    async def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        return EmbeddingResult(
            vectors=[self._vector(text) for text in texts],
            provider=self.provider,
            model=self.model,
            dimension=self.dimension,
            usage={},
        )

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for feature, count in Counter(_features(text)).items():
            digest = blake2b(feature.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "little") % self.dimension
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign * (1.0 + math.log(count))
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            vector[0] = 1.0
            return vector
        return [value / norm for value in vector]


# --- Ingestion ---------------------------------------------------------------


class _NoOcr:
    """Fixture manuals must be text-extractable; OCR would need a live model."""

    def render_png(self, pdf_path: Path, page_number: int) -> bytes:
        raise RuntimeError(
            f"{pdf_path.name} page {page_number} needs OCR; "
            "retrieval fixtures must contain extractable text"
        )

    async def extract_page(self, image_bytes: bytes, page_number: int):
        raise RuntimeError("retrieval fixtures must not require OCR")


@dataclass
class _IngestedCorpus:
    model_ids: dict[str, int]
    document_ids: dict[str, int]
    document_model: dict[int, str]
    run_ids: dict[str, int]
    chunk_stats: dict[str, dict[str, Any]]


async def _ingest(
    session: AsyncSession,
    dataset: RetrievalDataset,
    *,
    base_dir: Path,
    provider: EmbeddingProvider,
    model_name: str,
    min_text_chars: int,
) -> _IngestedCorpus:
    model_ids: dict[str, int] = {}
    for spec in dataset.device_models:
        model = DeviceModel(
            vendor_name=spec.vendor_name,
            model_code=spec.model_code,
            model_name=spec.model_name,
        )
        session.add(model)
        await session.flush()
        model_ids[spec.key] = model.id

    corpus = _IngestedCorpus(model_ids, {}, {}, {}, {})
    for spec in dataset.documents:
        path = base_dir / spec.path
        payload = path.read_bytes()
        document = SourceDocument(
            device_model_id=model_ids[spec.device_model],
            original_file_name=path.name,
            media_type=_MEDIA_TYPES[path.suffix.lower()],
            file_size_bytes=len(payload),
            file_hash_sha256=sha256(payload).hexdigest(),
            storage_uri=f"retrieval-eval/{spec.key}{path.suffix}",
            validation_status="validated",
        )
        session.add(document)
        await session.flush()
        corpus.document_ids[spec.key] = document.id
        corpus.document_model[document.id] = spec.device_model
        corpus.run_ids[spec.key] = await _ingest_run(
            session,
            document,
            path,
            provider=provider,
            model_name=model_name,
            min_text_chars=min_text_chars,
        )
        corpus.chunk_stats[spec.key] = await _chunk_stats(
            session, corpus.run_ids[spec.key]
        )
    return corpus


async def _ingest_run(
    session: AsyncSession,
    document: SourceDocument,
    path: Path,
    *,
    provider: EmbeddingProvider,
    model_name: str,
    min_text_chars: int,
    status: ExtractionRunStatus = ExtractionRunStatus.AWAITING_REVIEW,
    retry_of_run_id: int | None = None,
) -> int:
    run = ProfileExtractionRun(
        source_document_id=document.id,
        retry_of_run_id=retry_of_run_id,
        status=status,
        extraction_provider="retrieval-eval",
        extraction_model="none",
        embedding_provider=getattr(provider, "provider", "openai_compatible"),
        embedding_model=model_name,
    )
    session.add(run)
    await session.flush()
    if path.suffix.lower() == ".pdf":
        parsed = await parse_pdf(
            path, renderer=_NoOcr(), vision=_NoOcr(), min_text_chars=min_text_chars
        )
    else:
        parsed = parse_docx(path)
    for chunk in build_chunks(parsed):
        session.add(
            DocumentChunk(
                source_document_id=document.id,
                extraction_run_id=run.id,
                **chunk.__dict__,
            )
        )
    await session.flush()
    await embed_run_chunks(session, run.id, provider)
    return run.id


async def _chunk_stats(session: AsyncSession, run_id: int) -> dict[str, Any]:
    contents = list(
        (
            await session.scalars(
                select(DocumentChunk.content).where(
                    DocumentChunk.extraction_run_id == run_id
                )
            )
        ).all()
    )
    lengths = [len(content) for content in contents]
    return {
        "chunks": len(lengths),
        "mean_chars": round(mean(lengths), 1) if lengths else 0,
        "max_chars": max(lengths, default=0),
    }


# --- Scoring -----------------------------------------------------------------


class RetrievedChunk(BaseModel):
    rank: int
    stable_ref: str
    page_number: int | None
    similarity: float
    chars: int
    relevant: bool
    duplicate: bool
    out_of_scope: bool


class RetrievalCaseResult(BaseModel):
    id: str
    category: str
    query: str
    first_relevant_rank: int | None
    hit_at_1: bool
    hit_at_3: bool
    hit_at_k: bool
    reciprocal_rank: float
    evidence_recall_at_k: float
    context_chars_at_k: int
    duplicates_at_k: int
    out_of_scope_at_k: int
    latency_ms: float
    retrieved: list[RetrievedChunk]


class RetrievalMetrics(BaseModel):
    cases: int
    hit_at_1: float
    hit_at_3: float
    hit_at_k: float
    mrr: float
    evidence_recall_at_k: float
    mean_context_chars_at_k: float
    duplicate_rate_at_k: float
    out_of_scope_results: int
    mean_latency_ms: float


class RetrievalScenario(BaseModel):
    name: str
    description: str
    metrics: RetrievalMetrics
    by_category: dict[str, RetrievalMetrics]
    cases: list[RetrievalCaseResult]


class RetrievalReport(BaseModel):
    dataset_version: str
    k: int
    embedding_provider: str
    embedding_model: str
    corpus: dict[str, dict[str, Any]]
    scenarios: list[RetrievalScenario]


def score_case(
    case: RetrievalCase,
    results: Sequence[DocumentSearchResult],
    *,
    k: int,
    document_model: dict[int, str],
    latency_ms: float,
) -> RetrievalCaseResult:
    retrieved: list[RetrievedChunk] = []
    seen_content: set[str] = set()
    satisfied: set[int] = set()
    first_relevant_rank: int | None = None
    for rank, result in enumerate(results, start=1):
        matched = matches_evidence(result.content, case.evidence)
        normalized = normalize_text(result.content)
        duplicate = normalized in seen_content
        seen_content.add(normalized)
        if matched and first_relevant_rank is None:
            first_relevant_rank = rank
        if rank <= k:
            satisfied.update(matched)
        retrieved.append(
            RetrievedChunk(
                rank=rank,
                stable_ref=result.stable_ref,
                page_number=result.page_number,
                similarity=round(result.similarity, 4),
                # What the agent receives: the passage plus its section context.
                chars=len(result.content) + len(result.context_text),
                relevant=bool(matched),
                duplicate=duplicate,
                out_of_scope=document_model.get(result.source_document_id)
                != case.device_model,
            )
        )
    top = retrieved[:k]

    def hit(cutoff: int) -> bool:
        return first_relevant_rank is not None and first_relevant_rank <= cutoff

    return RetrievalCaseResult(
        id=case.id,
        category=case.category,
        query=case.query,
        first_relevant_rank=first_relevant_rank,
        hit_at_1=hit(1),
        hit_at_3=hit(3),
        hit_at_k=hit(k),
        reciprocal_rank=1.0 / first_relevant_rank if first_relevant_rank else 0.0,
        evidence_recall_at_k=len(satisfied) / len(case.evidence),
        context_chars_at_k=sum(item.chars for item in top),
        duplicates_at_k=sum(item.duplicate for item in top),
        out_of_scope_at_k=sum(item.out_of_scope for item in top),
        latency_ms=round(latency_ms, 1),
        retrieved=retrieved,
    )


def aggregate(results: Sequence[RetrievalCaseResult], *, k: int) -> RetrievalMetrics:
    def avg(values: Iterable[float]) -> float:
        values = list(values)
        return round(mean(values), 4) if values else 0.0

    returned = sum(min(len(result.retrieved), k) for result in results)
    return RetrievalMetrics(
        cases=len(results),
        hit_at_1=avg(float(result.hit_at_1) for result in results),
        hit_at_3=avg(float(result.hit_at_3) for result in results),
        hit_at_k=avg(float(result.hit_at_k) for result in results),
        mrr=avg(result.reciprocal_rank for result in results),
        evidence_recall_at_k=avg(result.evidence_recall_at_k for result in results),
        mean_context_chars_at_k=round(
            avg(result.context_chars_at_k for result in results), 1
        ),
        duplicate_rate_at_k=round(
            sum(result.duplicates_at_k for result in results) / returned, 4
        )
        if returned
        else 0.0,
        out_of_scope_results=sum(result.out_of_scope_at_k for result in results),
        mean_latency_ms=round(avg(result.latency_ms for result in results), 1),
    )


async def _run_cases(
    session: AsyncSession,
    dataset: RetrievalDataset,
    corpus: _IngestedCorpus,
    *,
    provider: EmbeddingProvider,
    k: int,
) -> list[RetrievalCaseResult]:
    results: list[RetrievalCaseResult] = []
    for case in dataset.cases:
        started = time.perf_counter()
        found = await search_document_chunks(
            session,
            query=case.query,
            source_document_id=None,
            device_model_id=corpus.model_ids[case.device_model],
            provider=provider,
            limit=max(k, SEARCH_DEPTH),
        )
        latency_ms = (time.perf_counter() - started) * 1000
        results.append(
            score_case(
                case,
                found,
                k=k,
                document_model=corpus.document_model,
                latency_ms=latency_ms,
            )
        )
    return results


def _scenario(
    name: str, description: str, results: list[RetrievalCaseResult], *, k: int
) -> RetrievalScenario:
    by_category: dict[str, list[RetrievalCaseResult]] = defaultdict(list)
    for result in results:
        by_category[result.category].append(result)
    return RetrievalScenario(
        name=name,
        description=description,
        metrics=aggregate(results, k=k),
        by_category={
            category: aggregate(items, k=k)
            for category, items in sorted(by_category.items())
        },
        cases=results,
    )


async def run_retrieval_evaluation(
    session: AsyncSession,
    dataset: RetrievalDataset,
    *,
    base_dir: Path,
    provider: EmbeddingProvider,
    embedding_model: str,
    k: int = DEFAULT_K,
    min_text_chars: int = 40,
    include_retry_scenario: bool = True,
) -> RetrievalReport:
    """Ingest the dataset's manuals and score every case.

    Writes rows through ``session`` without committing; callers decide whether
    to roll back (the CLI always does).
    """
    if k < 1:
        raise ValueError("k must be positive")
    corpus = await _ingest(
        session,
        dataset,
        base_dir=base_dir,
        provider=provider,
        model_name=embedding_model,
        min_text_chars=min_text_chars,
    )
    scenarios = [
        _scenario(
            "single_run",
            "Each manual ingested once, as after a successful extraction run.",
            await _run_cases(session, dataset, corpus, provider=provider, k=k),
            k=k,
        )
    ]
    if include_retry_scenario:
        for spec in dataset.documents:
            document = await session.get(
                SourceDocument, corpus.document_ids[spec.key]
            )
            first_run = await session.get(
                ProfileExtractionRun, corpus.run_ids[spec.key]
            )
            first_run.status = ExtractionRunStatus.FAILED
            await _ingest_run(
                session,
                document,
                base_dir / spec.path,
                provider=provider,
                model_name=embedding_model,
                min_text_chars=min_text_chars,
                retry_of_run_id=first_run.id,
            )
        scenarios.append(
            _scenario(
                "after_retry",
                "Each manual's first run failed after embedding and was retried, "
                "as retry_extraction_run does.",
                await _run_cases(session, dataset, corpus, provider=provider, k=k),
                k=k,
            )
        )
    return RetrievalReport(
        dataset_version=dataset.version,
        k=k,
        embedding_provider=getattr(provider, "provider", "openai_compatible"),
        embedding_model=embedding_model,
        corpus=corpus.chunk_stats,
        scenarios=scenarios,
    )


def format_report(report: RetrievalReport) -> str:
    k = report.k
    title = (
        f"Retrieval eval v{report.dataset_version} · "
        f"{report.embedding_provider}/{report.embedding_model} · k={k}"
    )
    lines = [
        title,
        "",
        "Corpus: "
        + ", ".join(
            f"{key} {stats['chunks']} chunks (mean {stats['mean_chars']} / "
            f"max {stats['max_chars']} chars)"
            for key, stats in report.corpus.items()
        ),
    ]
    header = (
        f"{'':<22}{'n':>4}{'hit@1':>8}{'hit@3':>8}{f'hit@{k}':>8}{'MRR':>7}"
        f"{f'ev.rec@{k}':>10}{f'ctx@{k}':>9}{'dup':>7}{'scope':>7}"
    )

    def row(label: str, metrics: RetrievalMetrics) -> str:
        return (
            f"{label:<22}{metrics.cases:>4}{metrics.hit_at_1:>8.2f}"
            f"{metrics.hit_at_3:>8.2f}{metrics.hit_at_k:>8.2f}{metrics.mrr:>7.2f}"
            f"{metrics.evidence_recall_at_k:>10.2f}"
            f"{metrics.mean_context_chars_at_k:>9.0f}"
            f"{metrics.duplicate_rate_at_k:>7.2f}{metrics.out_of_scope_results:>7}"
        )

    for scenario in report.scenarios:
        lines += ["", f"[{scenario.name}] {scenario.description}", header]
        lines.append(row("all", scenario.metrics))
        for category, metrics in scenario.by_category.items():
            lines.append(row(f"  {category}", metrics))
        misses = [case for case in scenario.cases if not case.hit_at_k]
        if misses:
            lines.append(f"  misses@{k}: " + ", ".join(case.id for case in misses))
    return "\n".join(lines)
