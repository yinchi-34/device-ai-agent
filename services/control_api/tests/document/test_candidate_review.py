import pytest
from sqlalchemy import select

from app.core.errors import DomainError
from app.modules.device.models import BasicCapability
from app.modules.document.models import (
    CandidateReviewEvent,
    DocumentChunk,
    ProfileExtractionCandidate,
    ProfileExtractionRun,
    SourceDocument,
)
from app.modules.document.service import DocumentService
from tests.document.test_extraction_pipeline import candidate_payload


async def make_candidate(db_session, *, status="valid"):
    document = SourceDocument(
        original_file_name="manual.docx",
        media_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        file_size_bytes=10,
        file_hash_sha256="a" * 64,
        storage_uri="original/manual.docx",
        validation_status="validated",
    )
    db_session.add_all(
        [document, BasicCapability(code="run_status", name="Run status")]
    )
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
    db_session.add(
        DocumentChunk(
            source_document_id=document.id,
            extraction_run_id=run.id,
            stable_ref="docx:block:1",
            content_type="text",
            content="Run status register 40001",
            block_index=1,
            extraction_method="docx_xml",
            content_hash="b" * 64,
            token_count=5,
        )
    )
    candidate = ProfileExtractionCandidate(
        extraction_run_id=run.id,
        original_payload=candidate_payload(),
        reviewed_payload=candidate_payload(),
        status=status,
        revision=1,
    )
    db_session.add(candidate)
    await db_session.flush()
    return candidate


async def test_candidate_patch_rejects_stale_revision(db_session):
    candidate = await make_candidate(db_session)
    service = DocumentService(db_session)

    updated = await service.update_candidate(
        candidate.id, revision=1, payload=candidate_payload()
    )
    with pytest.raises(DomainError) as excinfo:
        await service.update_candidate(
            candidate.id, revision=1, payload=candidate_payload()
        )

    assert updated.revision == 2
    assert excinfo.value.code == "candidate_revision_conflict"


async def test_candidate_field_patch_uses_same_revision_lock(db_session):
    candidate = await make_candidate(db_session)
    service = DocumentService(db_session)

    updated = await service.update_candidate_fields(
        candidate.id,
        revision=1,
        changes={"/profile/version/value": "2.0-fields"},
    )
    with pytest.raises(DomainError) as excinfo:
        await service.update_candidate_fields(
            candidate.id,
            revision=1,
            changes={"/profile/version/value": "stale"},
        )

    assert updated.reviewed_payload["profile"]["version"]["value"] == "2.0-fields"
    assert updated.revision == 2
    assert excinfo.value.code == "candidate_revision_conflict"


async def test_approval_requires_valid_candidate(db_session):
    candidate = await make_candidate(db_session, status="invalid")

    with pytest.raises(DomainError) as excinfo:
        await DocumentService(db_session).approve_candidate(
            candidate.id, revision=1
        )

    assert excinfo.value.code == "candidate_invalid"


async def test_approve_and_reject_record_review_state(db_session):
    candidate = await make_candidate(db_session)
    service = DocumentService(db_session)

    approved = await service.approve_candidate(candidate.id, revision=1)
    assert approved.status == "approved"
    rejected = await service.reject_candidate(
        candidate.id, revision=approved.revision, note="wrong model"
    )

    assert rejected.status == "rejected"
    assert rejected.review_note == "wrong model"
    assert rejected.reviewed_at is not None


async def test_review_actions_append_actor_revision_and_payload_audit(db_session):
    candidate = await make_candidate(db_session)
    service = DocumentService(db_session)

    saved = await service.update_candidate_fields(
        candidate.id,
        revision=1,
        changes={"/profile/version/value": "2.0-audited"},
        actor="reviewer",
    )
    await service.approve_candidate(
        candidate.id, revision=saved.revision, actor="reviewer"
    )
    profile = await service.import_candidate(candidate.id, actor="reviewer")

    events = list(
        (
            await db_session.scalars(
                select(CandidateReviewEvent).order_by(CandidateReviewEvent.id)
            )
        ).all()
    )
    assert [event.action for event in events] == ["saved", "approved", "imported"]
    assert {event.actor for event in events} == {"reviewer"}
    assert [event.revision for event in events] == [2, 2, 2]
    assert events[0].payload_snapshot["profile"]["version"]["value"] == (
        "2.0-audited"
    )
    assert events[-1].event_metadata == {"profile_version_id": profile.id}


async def test_reject_requires_note_and_approved_candidate_can_be_reopened(db_session):
    candidate = await make_candidate(db_session)
    service = DocumentService(db_session)

    with pytest.raises(DomainError) as excinfo:
        await service.reject_candidate(candidate.id, revision=1, note="", actor="reviewer")
    assert excinfo.value.code == "candidate_review_note_required"

    approved = await service.approve_candidate(
        candidate.id, revision=1, actor="reviewer"
    )
    reopened = await service.reopen_candidate(
        candidate.id, revision=approved.revision, actor="reviewer"
    )

    assert reopened.status == "valid"
    events = list(
        (
            await db_session.scalars(
                select(CandidateReviewEvent).order_by(CandidateReviewEvent.id)
            )
        ).all()
    )
    assert [event.action for event in events] == ["approved", "reopened"]
