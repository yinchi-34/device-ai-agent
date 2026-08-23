import pytest
from sqlalchemy import select

from app.core.errors import DomainError
from app.modules.document.models import (
    DocumentChunk,
    DocumentChunkEmbedding,
    ProfileExtractionRun,
    SourceDocument,
)
from app.modules.document.providers import EmbeddingResult, FakeEmbeddingProvider
from app.modules.document.retrieval import embed_run_chunks, search_document_chunks


def embedding_provider(vector):
    return FakeEmbeddingProvider(
        EmbeddingResult(
            vectors=[vector],
            provider="fake",
            model="embed-v1",
            dimension=len(vector),
            usage={},
        )
    )


async def make_document_with_chunk(db_session, stable_ref, content, vector):
    document = SourceDocument(
        original_file_name="manual.pdf",
        media_type="application/pdf",
        file_size_bytes=10,
        file_hash_sha256="a" * 64,
        storage_uri=f"original/{stable_ref}.pdf",
        validation_status="validated",
    )
    db_session.add(document)
    await db_session.flush()
    run = ProfileExtractionRun(
        source_document_id=document.id,
        status="awaiting_review",
        extraction_provider="fake",
        extraction_model="extract-v1",
        embedding_provider="fake",
        embedding_model="embed-v1",
    )
    db_session.add(run)
    await db_session.flush()
    chunk = DocumentChunk(
        source_document_id=document.id,
        extraction_run_id=run.id,
        stable_ref=stable_ref,
        content_type="text",
        content=content,
        page_number=1,
        block_index=1,
        extraction_method="pdf_text",
        content_hash="b" * 64,
        token_count=4,
    )
    db_session.add(chunk)
    await db_session.flush()
    db_session.add(
        DocumentChunkEmbedding(
            document_chunk_id=chunk.id,
            provider="fake",
            model="embed-v1",
            dimension=len(vector),
            embedding=vector,
        )
    )
    await db_session.flush()
    return document


async def test_search_requires_scope(db_session):
    with pytest.raises(DomainError) as excinfo:
        await search_document_chunks(
            db_session,
            query="run status",
            source_document_id=None,
            device_model_id=None,
            provider=embedding_provider([1.0, 0.0, 0.0]),
            limit=5,
        )

    assert excinfo.value.code == "document_search_scope_required"


async def test_search_returns_only_requested_document_with_evidence(db_session):
    first = await make_document_with_chunk(
        db_session,
        stable_ref="pdf:page:1:block:1",
        content="run status register",
        vector=[1.0, 0.0, 0.0],
    )
    await make_document_with_chunk(
        db_session,
        stable_ref="pdf:page:9:block:1",
        content="unrelated document",
        vector=[1.0, 0.0, 0.0],
    )

    results = await search_document_chunks(
        db_session,
        query="run status",
        source_document_id=first.id,
        device_model_id=None,
        provider=embedding_provider([1.0, 0.0, 0.0]),
        limit=5,
    )

    assert [(item.source_document_id, item.stable_ref) for item in results] == [
        (first.id, "pdf:page:1:block:1")
    ]
    assert results[0].similarity == pytest.approx(1.0)


class BatchEmbeddingProvider:
    def __init__(self):
        self.calls = []

    async def embed(self, texts):
        self.calls.append(list(texts))
        return EmbeddingResult(
            vectors=[[1.0, 0.0, 0.0] for _ in texts],
            provider="fake",
            model="embed-v1",
            dimension=3,
            usage={},
        )


async def test_embed_run_chunks_batches_large_documents(db_session):
    document = SourceDocument(
        original_file_name="manual.pdf",
        media_type="application/pdf",
        file_size_bytes=10,
        file_hash_sha256="a" * 64,
        storage_uri="original/manual.pdf",
        validation_status="validated",
    )
    db_session.add(document)
    await db_session.flush()
    run = ProfileExtractionRun(
        source_document_id=document.id,
        status="parsing",
        extraction_provider="fake",
        extraction_model="extract-v1",
        embedding_provider="fake",
        embedding_model="embed-v1",
    )
    db_session.add(run)
    await db_session.flush()
    for index in range(5):
        db_session.add(
            DocumentChunk(
                source_document_id=document.id,
                extraction_run_id=run.id,
                stable_ref=f"pdf:page:{index + 1}:block:{index + 1}",
                content_type="text",
                content=f"chunk {index}",
                page_number=index + 1,
                block_index=index + 1,
                extraction_method="pdf_text",
                content_hash=str(index) * 64,
                token_count=2,
            )
        )
    await db_session.flush()
    provider = BatchEmbeddingProvider()

    embeddings = await embed_run_chunks(db_session, run.id, provider, batch_size=2)

    assert [len(call) for call in provider.calls] == [2, 2, 1]
    assert len(embeddings) == 5


async def test_embed_run_chunks_skips_chunks_with_existing_embeddings(db_session):
    document = await make_document_with_chunk(
        db_session,
        stable_ref="pdf:page:1:block:1",
        content="already embedded",
        vector=[1.0, 0.0, 0.0],
    )
    run_id = await db_session.scalar(
        select(ProfileExtractionRun.id).where(
            ProfileExtractionRun.source_document_id == document.id
        )
    )
    provider = BatchEmbeddingProvider()

    embeddings = await embed_run_chunks(db_session, run_id, provider)

    assert provider.calls == []
    assert len(embeddings) == 1
