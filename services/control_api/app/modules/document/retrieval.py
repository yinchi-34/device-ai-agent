from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
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
    chunks = list((await session.scalars(statement)).all())
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
    missing_chunks = [
        chunk for chunk in chunks if chunk.id not in existing_by_chunk_id
    ]
    if not missing_chunks:
        return [existing_by_chunk_id[chunk.id] for chunk in chunks]

    vectors: list[list[float]] = []
    result = None
    for start in range(0, len(missing_chunks), batch_size):
        batch = missing_chunks[start : start + batch_size]
        batch_result = await provider.embed([chunk.content for chunk in batch])
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
    statement = (
        select(DocumentChunk, distance)
        .join(
            DocumentChunkEmbedding,
            DocumentChunkEmbedding.document_chunk_id == DocumentChunk.id,
        )
        .join(SourceDocument, SourceDocument.id == DocumentChunk.source_document_id)
        .where(
            DocumentChunkEmbedding.provider == result.provider,
            DocumentChunkEmbedding.model == result.model,
            DocumentChunkEmbedding.dimension == result.dimension,
            SourceDocument.is_deleted.is_(False),
        )
    )
    if source_document_id is not None:
        statement = statement.where(
            DocumentChunk.source_document_id == source_document_id
        )
    if device_model_id is not None:
        statement = statement.where(SourceDocument.device_model_id == device_model_id)
    statement = statement.order_by(distance, DocumentChunk.id).limit(
        max(1, min(limit, 50))
    )
    rows = (await session.execute(statement)).all()
    return [
        DocumentSearchResult(
            chunk_id=chunk.id,
            source_document_id=chunk.source_document_id,
            stable_ref=chunk.stable_ref,
            page_number=chunk.page_number,
            content=chunk.content,
            similarity=1.0 - float(row_distance),
        )
        for chunk, row_distance in rows
    ]
