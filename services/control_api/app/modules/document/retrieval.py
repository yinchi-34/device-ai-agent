from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.modules.document.models import (
    DocumentChunk,
    DocumentChunkEmbedding,
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
) -> list[DocumentChunkEmbedding]:
    statement = (
        select(DocumentChunk)
        .where(DocumentChunk.extraction_run_id == run_id)
        .order_by(DocumentChunk.block_index, DocumentChunk.id)
    )
    chunks = list((await session.scalars(statement)).all())
    if not chunks:
        return []
    result = await provider.embed([chunk.content for chunk in chunks])
    if len(result.vectors) != len(chunks):
        raise ProviderError("provider_embedding_count_mismatch")
    embeddings: list[DocumentChunkEmbedding] = []
    for chunk, vector in zip(chunks, result.vectors, strict=True):
        existing = await session.scalar(
            select(DocumentChunkEmbedding).where(
                DocumentChunkEmbedding.document_chunk_id == chunk.id,
                DocumentChunkEmbedding.provider == result.provider,
                DocumentChunkEmbedding.model == result.model,
            )
        )
        if existing is not None:
            embeddings.append(existing)
            continue
        embedding = DocumentChunkEmbedding(
            document_chunk_id=chunk.id,
            provider=result.provider,
            model=result.model,
            dimension=result.dimension,
            embedding=vector,
        )
        session.add(embedding)
        embeddings.append(embedding)
    await session.flush()
    return embeddings


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
