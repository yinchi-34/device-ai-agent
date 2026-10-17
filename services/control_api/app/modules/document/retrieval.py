import re
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.modules.document.chunking import embedding_text, is_context_only
from app.modules.document.models import (
    DocumentChunk,
    DocumentChunkEmbedding,
    ProfileExtractionRun,
    SourceDocument,
)
from app.modules.document.providers import EmbeddingProvider, ProviderError


@dataclass(frozen=True)
class DocumentSearchResult:
    chunk_id: int
    source_document_id: int
    stable_ref: str
    page_number: int | None
    content: str
    similarity: float
    context_text: str = ""
    matched_codes: tuple[str, ...] = ()


# Identifiers that embeddings match poorly: alarm/fault codes (A05, E05,
# AL-12), parameter numbers (P0.05) and 5-6 digit register addresses (30011,
# 00002). Shorter bare numbers are values ("250 Pa"), not identifiers.
_CODE_BOUNDARY_BEFORE = r"(?<![A-Za-z0-9.])"
_QUERY_CODE = re.compile(
    _CODE_BOUNDARY_BEFORE
    + r"(?:[A-Za-z]{1,3}-?\d{1,4}(?:\.\d{1,3})?(?![A-Za-z0-9])|\d{5,6}(?![0-9.]))"
)
_RRF_K = 60


def extract_query_codes(query: str) -> list[str]:
    codes: list[str] = []
    for match in _QUERY_CODE.finditer(query):
        code = match.group(0).upper()
        if code not in codes:
            codes.append(code)
    return codes


def _escape_code(code: str) -> str:
    return "".join(char if char.isalnum() else f"\\{char}" for char in code)


def _postgres_code_pattern(code: str) -> str:
    return f"(^|[^A-Za-z0-9.]){_escape_code(code)}([^A-Za-z0-9]|$)"


def _codes_in(content: str, codes: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        code
        for code in codes
        if re.search(
            _CODE_BOUNDARY_BEFORE + re.escape(code) + r"(?![A-Za-z0-9])",
            content,
            re.IGNORECASE,
        )
    )


async def embed_run_chunks(
    session: AsyncSession,
    run_id: int,
    provider: EmbeddingProvider,
    *,
    batch_size: int = 64,
) -> list[DocumentChunkEmbedding]:
    statement = (
        select(DocumentChunk)
        .where(DocumentChunk.extraction_run_id == run_id)
        .order_by(DocumentChunk.block_index, DocumentChunk.id)
    )
    chunks = [
        chunk
        for chunk in (await session.scalars(statement)).all()
        if not is_context_only(chunk.content_type, chunk.table_metadata)
    ]
    if not chunks:
        return []
    if batch_size < 1:
        raise ValueError("batch_size must be positive")

    run = await session.get(ProfileExtractionRun, run_id)
    if run is None:
        raise ValueError(f"Extraction run {run_id} not found")
    existing_statement = (
        select(DocumentChunkEmbedding)
        .join(
            DocumentChunk,
            DocumentChunk.id == DocumentChunkEmbedding.document_chunk_id,
        )
        .where(
            DocumentChunk.extraction_run_id == run_id,
            DocumentChunkEmbedding.provider == run.embedding_provider,
            DocumentChunkEmbedding.model == run.embedding_model,
        )
    )
    existing_by_chunk_id = {
        embedding.document_chunk_id: embedding
        for embedding in (await session.scalars(existing_statement)).all()
    }
    missing_chunks = [chunk for chunk in chunks if chunk.id not in existing_by_chunk_id]
    if not missing_chunks:
        return [existing_by_chunk_id[chunk.id] for chunk in chunks]

    vectors: list[list[float]] = []
    result = None
    for start in range(0, len(missing_chunks), batch_size):
        batch = missing_chunks[start : start + batch_size]
        batch_result = await provider.embed(
            [embedding_text(chunk.content, chunk.context_text) for chunk in batch]
        )
        if len(batch_result.vectors) != len(batch):
            raise ProviderError("provider_embedding_count_mismatch")
        if (batch_result.provider, batch_result.model) != (
            run.embedding_provider,
            run.embedding_model,
        ):
            raise ProviderError("provider_embedding_metadata_mismatch")
        if result is not None and (
            batch_result.provider,
            batch_result.model,
            batch_result.dimension,
        ) != (result.provider, result.model, result.dimension):
            raise ProviderError("provider_embedding_metadata_mismatch")
        result = batch_result
        vectors.extend(batch_result.vectors)
    if result is None:
        raise RuntimeError("embedding batches did not execute")

    for chunk, vector in zip(missing_chunks, vectors, strict=True):
        embedding = DocumentChunkEmbedding(
            document_chunk_id=chunk.id,
            provider=result.provider,
            model=result.model,
            dimension=result.dimension,
            embedding=vector,
        )
        session.add(embedding)
        existing_by_chunk_id[chunk.id] = embedding
    await session.flush()
    return [existing_by_chunk_id[chunk.id] for chunk in chunks]


async def search_document_chunks(
    session: AsyncSession,
    *,
    query: str,
    source_document_id: int | None,
    device_model_id: int | None,
    provider: EmbeddingProvider,
    limit: int = 10,
) -> Sequence[DocumentSearchResult]:
    if source_document_id is None and device_model_id is None:
        raise DomainError(
            "document_search_scope_required",
            "source_document_id or device_model_id is required",
            status_code=422,
        )
    result = await provider.embed([query])
    if len(result.vectors) != 1:
        raise ProviderError("provider_embedding_count_mismatch")
    query_vector = result.vectors[0]
    distance = DocumentChunkEmbedding.embedding.cosine_distance(query_vector).label(
        "distance"
    )
    embedding_matches = (
        DocumentChunkEmbedding.provider == result.provider,
        DocumentChunkEmbedding.model == result.model,
        DocumentChunkEmbedding.dimension == result.dimension,
    )
    # Every extraction run (including retries) re-chunks the whole document, so
    # searching all runs returns the same passage once per run. Search only the
    # newest run per document that has finished embedding with this model; a
    # newer run that is still parsing does not hide the document meanwhile.
    latest_runs = (
        select(
            DocumentChunk.source_document_id.label("source_document_id"),
            func.max(DocumentChunk.extraction_run_id).label("extraction_run_id"),
        )
        .join(
            DocumentChunkEmbedding,
            DocumentChunkEmbedding.document_chunk_id == DocumentChunk.id,
        )
        .where(*embedding_matches)
        .group_by(DocumentChunk.source_document_id)
        .subquery()
    )
    statement = (
        select(DocumentChunk, distance)
        .join(
            DocumentChunkEmbedding,
            DocumentChunkEmbedding.document_chunk_id == DocumentChunk.id,
        )
        .join(
            latest_runs,
            and_(
                latest_runs.c.source_document_id == DocumentChunk.source_document_id,
                latest_runs.c.extraction_run_id == DocumentChunk.extraction_run_id,
            ),
        )
        .join(SourceDocument, SourceDocument.id == DocumentChunk.source_document_id)
        .where(
            *embedding_matches,
            SourceDocument.is_deleted.is_(False),
        )
    )
    if source_document_id is not None:
        statement = statement.where(
            DocumentChunk.source_document_id == source_document_id
        )
    if device_model_id is not None:
        statement = statement.where(SourceDocument.device_model_id == device_model_id)
    limit = max(1, min(limit, 50))
    codes = extract_query_codes(query)
    if not codes:
        rows = (
            await session.execute(
                statement.order_by(distance, DocumentChunk.id).limit(limit)
            )
        ).all()
        return [_search_result(chunk, row_distance, ()) for chunk, row_distance in rows]

    # Hybrid: fuse the vector ranking with an exact-identifier ranking by
    # reciprocal rank fusion. Exact matches that define the code (the chunk
    # starts with it, e.g. an alarm table row) rank ahead of mere mentions.
    candidates = max(limit * 4, 20)
    vector_rows = (
        await session.execute(
            statement.order_by(distance, DocumentChunk.id).limit(candidates)
        )
    ).all()
    code_condition = or_(
        *(
            DocumentChunk.content.op("~*")(_postgres_code_pattern(code))
            for code in codes
        )
    )
    code_rows = (
        await session.execute(
            statement.where(code_condition)
            .order_by(distance, DocumentChunk.id)
            .limit(candidates)
        )
    ).all()

    rows_by_id = {
        chunk.id: (chunk, row_distance) for chunk, row_distance in vector_rows
    }
    rows_by_id.update(
        {chunk.id: (chunk, row_distance) for chunk, row_distance in code_rows}
    )
    matched = {chunk.id: _codes_in(chunk.content, codes) for chunk, _ in code_rows}

    def defines_code(chunk: DocumentChunk) -> bool:
        head = chunk.content.lstrip().upper()
        return any(head.startswith(code) for code in matched.get(chunk.id, ()))

    ranked_codes = sorted(
        code_rows,
        key=lambda row: (
            not defines_code(row[0]),
            -len(matched[row[0].id]),
            float(row[1]),
            row[0].id,
        ),
    )
    scores: dict[int, float] = {}
    vector_rank = {chunk.id: rank for rank, (chunk, _) in enumerate(vector_rows, 1)}
    for chunk_id in rows_by_id:
        # A chunk beyond the vector candidates still has a (low) vector rank.
        rank = vector_rank.get(chunk_id, len(vector_rows) + 1)
        scores[chunk_id] = 1.0 / (_RRF_K + rank)
    for rank, (chunk, _) in enumerate(ranked_codes, start=1):
        scores[chunk.id] += 1.0 / (_RRF_K + rank)

    ordered = sorted(
        rows_by_id.values(),
        key=lambda row: (-scores[row[0].id], float(row[1]), row[0].id),
    )
    return [
        _search_result(chunk, row_distance, matched.get(chunk.id, ()))
        for chunk, row_distance in ordered[:limit]
    ]


def _search_result(
    chunk: DocumentChunk, row_distance: float, matched_codes: tuple[str, ...]
) -> DocumentSearchResult:
    return DocumentSearchResult(
        chunk_id=chunk.id,
        source_document_id=chunk.source_document_id,
        stable_ref=chunk.stable_ref,
        page_number=chunk.page_number,
        content=chunk.content,
        similarity=1.0 - float(row_distance),
        context_text=chunk.context_text,
        matched_codes=matched_codes,
    )
