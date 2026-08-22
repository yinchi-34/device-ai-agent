import pytest
from sqlalchemy.exc import IntegrityError

from app.modules.document.enums import CandidateStatus, ExtractionRunStatus
from app.modules.document.models import (
    ProfileExtractionCandidate,
    ProfileExtractionRun,
    SourceDocument,
)


def make_document() -> SourceDocument:
    return SourceDocument(
        original_file_name="manual.pdf",
        media_type="application/pdf",
        file_size_bytes=10,
        file_hash_sha256="a" * 64,
        storage_uri="original/aa/manual.pdf",
        validation_status="validated",
    )


def make_run(document_id: int, **overrides) -> ProfileExtractionRun:
    values = {
        "source_document_id": document_id,
        "status": ExtractionRunStatus.QUEUED,
        "attempt_number": 1,
        "parser_version": "1",
        "prompt_version": "1",
        "candidate_schema_version": "1",
        "extraction_provider": "fake",
        "extraction_model": "fake",
        "embedding_provider": "fake",
        "embedding_model": "fake",
    }
    values.update(overrides)
    return ProfileExtractionRun(**values)


async def test_one_run_has_at_most_one_candidate(db_session):
    document = make_document()
    db_session.add(document)
    await db_session.flush()
    run = make_run(document.id)
    db_session.add(run)
    await db_session.flush()
    db_session.add_all(
        [
            ProfileExtractionCandidate(
                extraction_run_id=run.id,
                original_payload={},
                reviewed_payload={},
                status=CandidateStatus.PENDING,
                revision=1,
            ),
            ProfileExtractionCandidate(
                extraction_run_id=run.id,
                original_payload={},
                reviewed_payload={},
                status=CandidateStatus.PENDING,
                revision=1,
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_retry_run_links_without_overwriting_failure(db_session):
    document = make_document()
    db_session.add(document)
    await db_session.flush()
    failed = make_run(
        document.id,
        status=ExtractionRunStatus.FAILED,
        error_code="provider_timeout",
    )
    db_session.add(failed)
    await db_session.flush()
    retry = make_run(
        document.id,
        retry_of_run_id=failed.id,
        status=ExtractionRunStatus.QUEUED,
        attempt_number=2,
    )
    db_session.add(retry)
    await db_session.flush()

    assert retry.retry_of_run_id == failed.id
    assert failed.status == ExtractionRunStatus.FAILED
    assert retry.status == ExtractionRunStatus.QUEUED
    assert failed.error_code == "provider_timeout"
