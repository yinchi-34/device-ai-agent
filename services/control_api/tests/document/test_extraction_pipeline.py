from copy import deepcopy
from hashlib import sha256
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from sqlalchemy import func, select

from app.db.session import async_session_factory
from app.modules.device.models import BasicCapability
from app.modules.document.chunking import NormalizedChunk
from app.modules.document.extraction import (
    _extract_grouped_candidate,
    _release_run_lock,
    _try_acquire_run_lock,
    execute_extraction_run,
    execute_extraction_with_retries,
)
from app.modules.document.models import (
    DocumentChunk,
    ProfileExtractionCandidate,
    SourceDocument,
)
from app.modules.document.providers import (
    EmbeddingResult,
    ExtractionResult,
    FakeEmbeddingProvider,
    FakeExtractionProvider,
    FakeVisionOcrProvider,
    ProviderError,
)
from app.modules.document.service import create_extraction_run
from app.modules.document.storage import FileSystemDocumentStorage


def sourced(value):
    return {
        "value": value,
        "confidence": 0.95,
        "evidence_refs": ["docx:block:1"],
    }


def candidate_payload():
    return {
        "schema_version": "1.0",
        "device_model": {
            "vendor_name": sourced("Acme"),
            "model_code": sourced("M-1"),
            "model_name": sourced("Meter"),
        },
        "profile": {"version": sourced("1.0")},
        "protocols": [
            {
                "candidate_ref": "protocol:modbus",
                "code": sourced("modbus"),
                "protocol": sourced("modbus_tcp"),
                "config_schema": {},
            }
        ],
        "points": [],
        "capability_matches": [
            {
                "candidate_ref": "capability:run_status",
                "capability_code": "run_status",
                "confidence": 0.9,
                "evidence_refs": ["docx:block:1"],
            }
        ],
        "point_bindings": [],
        "unresolved_suggestions": [],
    }


def create_docx(path):
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body><w:p><w:r><w:t>Run status register 40001</w:t></w:r></w:p></w:body>
    </w:document>"""
    path.parent.mkdir(parents=True)
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
    return path


async def pipeline_setup(db_session, tmp_path):
    path = create_docx(tmp_path / "original" / "manual.docx")
    content = path.read_bytes()
    document = SourceDocument(
        original_file_name="manual.docx",
        media_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        file_size_bytes=len(content),
        file_hash_sha256=sha256(content).hexdigest(),
        storage_uri="original/manual.docx",
        validation_status="validated",
    )
    db_session.add_all(
        [
            document,
            BasicCapability(code="run_status", name="Run status"),
        ]
    )
    await db_session.flush()
    run = await create_extraction_run(
        db_session,
        document.id,
        extraction_provider="fake",
        extraction_model="extract-v1",
        embedding_provider="fake",
        embedding_model="embed-v1",
    )
    extraction = FakeExtractionProvider(
        ExtractionResult(candidate_payload(), {"id": "response"}, {})
    )
    embedding = FakeEmbeddingProvider(
        EmbeddingResult([[1.0, 0.0, 0.0]], "fake", "embed-v1", 3, {})
    )
    return run, extraction, embedding


async def test_pipeline_reaches_awaiting_review(db_session, tmp_path):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    assert run.prompt_version == "5"

    await execute_extraction_run(
        db_session,
        run.id,
        storage=FileSystemDocumentStorage(tmp_path),
        extraction_provider=extraction,
        vision_provider=FakeVisionOcrProvider([]),
        embedding_provider=embedding,
    )

    await db_session.refresh(run, attribute_names=["candidate"])
    assert run.status == "awaiting_review"
    assert run.candidate.reviewed_payload == run.candidate.original_payload
    assert run.candidate.status == "valid"


async def test_pipeline_clears_stale_error_before_provider_call(db_session, tmp_path):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    run.status = "failed"
    run.error_code = "provider_timeout"
    run.error_detail = {"retryable": True}
    await db_session.flush()

    class CheckingExtractionProvider:
        async def extract(self, chunks, context):
            assert run.error_code is None
            assert run.error_detail == {}
            return await extraction.extract(chunks, context)

    await execute_extraction_run(
        db_session,
        run.id,
        storage=FileSystemDocumentStorage(tmp_path),
        extraction_provider=CheckingExtractionProvider(),
        vision_provider=FakeVisionOcrProvider([]),
        embedding_provider=embedding,
    )


async def test_pipeline_ignores_duplicate_delivery_while_run_is_locked(
    db_session, tmp_path
):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    await db_session.commit()
    lock_connection = await _try_acquire_run_lock(db_session, run.id)
    assert lock_connection is not None

    try:
        async with async_session_factory() as competing_session:
            competing_run = await execute_extraction_run(
                competing_session,
                run.id,
                storage=FileSystemDocumentStorage(tmp_path),
                extraction_provider=extraction,
                vision_provider=FakeVisionOcrProvider([]),
                embedding_provider=embedding,
            )
    finally:
        await _release_run_lock(lock_connection, run.id)

    assert competing_run.status == "queued"
    assert extraction.call_count == 0


async def test_transient_errors_retry_without_duplicate_candidate(db_session, tmp_path):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [
        ProviderError("provider_timeout", retryable=True),
        ProviderError("provider_timeout", retryable=True),
    ]

    await execute_extraction_with_retries(
        db_session,
        run.id,
        storage=FileSystemDocumentStorage(tmp_path),
        extraction_provider=extraction,
        vision_provider=FakeVisionOcrProvider([]),
        embedding_provider=embedding,
        max_attempts=3,
    )

    count = await db_session.scalar(select(func.count(ProfileExtractionCandidate.id)))
    await db_session.refresh(run)
    assert extraction.call_count == 3
    assert count == 1


async def test_pipeline_reuses_persisted_chunks_on_retry(db_session, tmp_path):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    db_session.add(
        DocumentChunk(
            source_document_id=run.source_document_id,
            extraction_run_id=run.id,
            stable_ref="docx:block:1",
            content_type="text",
            content="cached parsed content",
            block_index=1,
            extraction_method="docx_xml",
            content_hash="c" * 64,
            token_count=5,
        )
    )
    run.parse_metadata = {"block_count": 1}
    run.status = "failed"
    await db_session.flush()

    await execute_extraction_run(
        db_session,
        run.id,
        storage=FileSystemDocumentStorage(tmp_path),
        extraction_provider=extraction,
        vision_provider=FakeVisionOcrProvider([]),
        embedding_provider=embedding,
    )

    chunks, _ = extraction.calls[0]
    assert [chunk.content for chunk in chunks] == ["cached parsed content"]
    assert run.status == "awaiting_review"


def normalized_chunk(index):
    return NormalizedChunk(
        stable_ref=f"docx:block:{index}",
        content_type="text",
        content=f"chunk {index}",
        page_number=None,
        block_index=index,
        bounding_box=None,
        table_metadata={},
        extraction_method="docx_xml",
        extraction_confidence=None,
        content_hash=str(index) * 64,
        token_count=5,
    )


async def test_grouped_agent_checkpoints_and_resumes_fragments(db_session, tmp_path):
    run, _, _ = await pipeline_setup(db_session, tmp_path)
    provider = FakeExtractionProvider(
        ExtractionResult(
            candidate_payload(),
            {"id": "fragment-response"},
            {"total_tokens": 10},
        )
    )
    chunks = [normalized_chunk(1), normalized_chunk(2)]

    result = await _extract_grouped_candidate(
        db_session,
        run,
        provider,
        chunks,
        {"source_document_id": run.source_document_id},
        max_tokens=5,
    )

    assert provider.call_count == 2
    assert [call[1]["group_index"] for call in provider.calls] == [1, 2]
    assert all(call[1]["extraction_mode"] == "fragment" for call in provider.calls)
    await db_session.refresh(run)
    assert len(run.raw_agent_response["fragments"]) == 2
    assert result.usage["total_tokens"] == 20

    resumed_provider = FakeExtractionProvider(provider.result)
    resumed = await _extract_grouped_candidate(
        db_session,
        run,
        resumed_provider,
        chunks,
        {"source_document_id": run.source_document_id},
        max_tokens=5,
    )

    assert resumed_provider.call_count == 0
    assert resumed.payload == result.payload

    checkpoint = deepcopy(run.raw_agent_response)
    checkpoint["fragments"][1]["payload"] = {"schema_version": "1.0"}
    run.raw_agent_response = checkpoint
    await db_session.commit()

    repair_provider = FakeExtractionProvider(provider.result)
    repaired = await _extract_grouped_candidate(
        db_session,
        run,
        repair_provider,
        chunks,
        {"source_document_id": run.source_document_id},
        max_tokens=5,
    )

    assert repair_provider.call_count == 1
    await db_session.refresh(run)
    assert len(run.raw_agent_response["fragments"]) == 2
    assert repaired.payload == result.payload


@pytest.mark.parametrize(
    "split_error_code",
    ["provider_invalid_fragment", "provider_connection_error"],
)
async def test_grouped_agent_splits_structurally_invalid_groups(
    db_session, tmp_path, split_error_code
):
    run, _, _ = await pipeline_setup(db_session, tmp_path)

    class SplitProvider:
        def __init__(self):
            self.calls = []

        async def extract(self, chunks, context):
            self.calls.append((list(chunks), context))
            if len(chunks) > 1:
                raise ProviderError(split_error_code, retryable=True)
            return ExtractionResult(
                candidate_payload(),
                {"id": f"part-{chunks[0].block_index}"},
                {"total_tokens": 5},
            )

    provider = SplitProvider()
    chunks = [normalized_chunk(1), normalized_chunk(2)]

    result = await _extract_grouped_candidate(
        db_session,
        run,
        provider,
        chunks,
        {"source_document_id": run.source_document_id},
        max_tokens=10,
    )

    assert [len(call[0]) for call in provider.calls] == [2, 1, 1]
    assert result.usage["total_tokens"] == 10
    await db_session.refresh(run)
    assert len(run.raw_agent_response["fragments"]) == 1


async def test_grouped_agent_proactively_splits_dense_groups(db_session, tmp_path):
    run, _, _ = await pipeline_setup(db_session, tmp_path)

    class RecordingProvider:
        def __init__(self):
            self.calls = []

        async def extract(self, chunks, context):
            self.calls.append(list(chunks))
            return ExtractionResult(
                candidate_payload(),
                {"id": f"part-{chunks[0].block_index}"},
                {"total_tokens": 5},
            )

    provider = RecordingProvider()
    first = normalized_chunk(1)
    second = normalized_chunk(2)
    chunks = [
        first.__class__(**{**first.__dict__, "token_count": 2000}),
        second.__class__(**{**second.__dict__, "token_count": 2000}),
    ]

    result = await _extract_grouped_candidate(
        db_session,
        run,
        provider,
        chunks,
        {"source_document_id": run.source_document_id},
        max_tokens=5000,
    )

    assert [len(call) for call in provider.calls] == [1, 1]
    assert result.usage["total_tokens"] == 10


@pytest.mark.parametrize(
    "leaf_error_code",
    ["provider_invalid_fragment", "provider_connection_error", "provider_timeout"],
)
async def test_grouped_agent_records_unresolved_single_chunk_failures(
    db_session, tmp_path, leaf_error_code
):
    run, _, _ = await pipeline_setup(db_session, tmp_path)

    class InvalidProvider:
        async def extract(self, chunks, context):
            raise ProviderError(leaf_error_code, retryable=True)

    result = await _extract_grouped_candidate(
        db_session,
        run,
        InvalidProvider(),
        [normalized_chunk(1), normalized_chunk(2)],
        {"source_document_id": run.source_document_id},
        max_tokens=10,
    )

    assert result.payload["points"] == []
    assert {
        item["evidence_refs"][0] for item in result.payload["unresolved_suggestions"]
    } == {"docx:block:1", "docx:block:2"}
    assert {
        item["error_code"] for item in result.payload["unresolved_suggestions"]
    } == {leaf_error_code}


async def test_grouped_agent_resumes_successful_recursive_parts(db_session, tmp_path):
    run, _, _ = await pipeline_setup(db_session, tmp_path)

    class FlakySplitProvider:
        def __init__(self):
            self.calls = []
            self.failed = False

        async def extract(self, chunks, context):
            self.calls.append([chunk.block_index for chunk in chunks])
            if chunks[0].block_index == 2 and not self.failed:
                self.failed = True
                raise ProviderError("provider_rate_limited", retryable=True)
            return ExtractionResult(
                candidate_payload(),
                {"id": f"part-{chunks[0].block_index}"},
                {"total_tokens": 5},
            )

    first = normalized_chunk(1)
    second = normalized_chunk(2)
    chunks = [
        first.__class__(**{**first.__dict__, "token_count": 2000}),
        second.__class__(**{**second.__dict__, "token_count": 2000}),
    ]
    provider = FlakySplitProvider()

    with pytest.raises(ProviderError):
        await _extract_grouped_candidate(
            db_session,
            run,
            provider,
            chunks,
            {"source_document_id": run.source_document_id},
            max_tokens=5000,
        )

    await db_session.refresh(run)
    assert "root.left" in run.raw_agent_response["in_progress_fragment"]["parts"]

    result = await _extract_grouped_candidate(
        db_session,
        run,
        provider,
        chunks,
        {"source_document_id": run.source_document_id},
        max_tokens=5000,
    )

    assert provider.calls == [[1], [2], [2]]
    assert len(run.raw_agent_response["fragments"]) == 1
    assert result.usage["total_tokens"] == 10
    assert "in_progress_fragment" not in run.raw_agent_response


def _run_kwargs(tmp_path, extraction, embedding, **extra):
    return {
        "storage": FileSystemDocumentStorage(tmp_path),
        "extraction_provider": extraction,
        "vision_provider": FakeVisionOcrProvider([]),
        "embedding_provider": embedding,
        **extra,
    }


async def test_retryable_error_schedules_retry_then_fails_after_max(
    db_session, tmp_path
):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [
        ProviderError("provider_rate_limited", retryable=True) for _ in range(4)
    ]

    for retries in range(3):
        with pytest.raises(ProviderError):
            await execute_extraction_run(
                db_session,
                run.id,
                **_run_kwargs(
                    tmp_path, extraction, embedding, retries=retries, max_retries=3
                ),
            )
        await db_session.refresh(run)
        assert run.status == "retry_scheduled"
        assert run.error_code == "provider_rate_limited"
        assert run.error_detail["retry_count"] == retries + 1
        assert run.error_detail["max_retries"] == 3
        assert run.finished_at is None

    with pytest.raises(ProviderError):
        await execute_extraction_run(
            db_session,
            run.id,
            **_run_kwargs(tmp_path, extraction, embedding, retries=3, max_retries=3),
        )
    await db_session.refresh(run)
    assert run.status == "failed"
    assert run.error_code == "provider_rate_limited"
    assert run.error_detail["retry_count"] == 3
    assert run.error_detail["max_retries"] == 3
    assert run.finished_at is not None


async def test_retry_scheduled_run_completes_when_retry_succeeds(
    db_session, tmp_path
):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [ProviderError("provider_rate_limited", retryable=True)]
    kwargs = _run_kwargs(tmp_path, extraction, embedding, max_retries=3)

    with pytest.raises(ProviderError):
        await execute_extraction_run(db_session, run.id, retries=0, **kwargs)
    await execute_extraction_run(db_session, run.id, retries=1, **kwargs)

    await db_session.refresh(run)
    assert run.status == "awaiting_review"
    assert run.error_code is None
    assert run.error_detail == {}


async def test_non_retryable_error_fails_immediately(db_session, tmp_path):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [ProviderError("provider_auth_error", retryable=False)]

    with pytest.raises(ProviderError):
        await execute_extraction_run(
            db_session,
            run.id,
            **_run_kwargs(tmp_path, extraction, embedding, retries=0, max_retries=3),
        )

    await db_session.refresh(run)
    assert run.status == "failed"
    assert run.error_code == "provider_auth_error"
    assert "retry_count" not in run.error_detail
