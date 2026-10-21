import asyncio
from io import BytesIO

import pytest
from pypdf import PdfWriter
from sqlalchemy import func, select

from app.modules.document import router as document_router
from app.modules.document.models import ProfileExtractionRun, SourceDocument
from tests.document.test_candidate_review import make_candidate
from tests.document.test_extraction_pipeline import candidate_payload


def valid_pdf_bytes():
    stream = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(stream)
    return stream.getvalue()


async def test_upload_then_queue_extraction(client, monkeypatch, tmp_path):
    monkeypatch.setenv("DEVICE_DOCUMENT_STORAGE_ROOT", str(tmp_path))
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)

    response = await client.post(
        "/api/v1/documents",
        files={"file": ("manual.pdf", valid_pdf_bytes(), "application/pdf")},
    )

    assert response.status_code == 201
    assert "storage_uri" not in response.json()
    document_id = response.json()["id"]
    queued = await client.post(
        f"/api/v1/documents/{document_id}/extraction-runs"
    )
    fetched = await client.get(f"/api/v1/extraction-runs/{queued.json()['id']}")

    assert queued.status_code == 202
    assert queued.json()["status"] == "queued"
    assert fetched.status_code == 200
    assert dispatched == [queued.json()["id"]]


async def test_upload_rejects_mismatched_content(client, tmp_path, monkeypatch):
    monkeypatch.setenv("DEVICE_DOCUMENT_STORAGE_ROOT", str(tmp_path))

    response = await client.post(
        "/api/v1/documents",
        files={"file": ("manual.pdf", b"not a pdf", "application/pdf")},
    )

    assert response.status_code == 422
    assert response.json()["code"] in {
        "document_pdf_invalid",
        "document_type_mismatch",
    }


async def test_candidate_patch_conflict_and_import_idempotency(
    client, db_session
):
    candidate = await make_candidate(db_session, status="approved")
    await db_session.commit()

    fetched = await client.get(
        f"/api/v1/extraction-runs/{candidate.extraction_run_id}/candidate"
    )
    first_import = await client.post(
        f"/api/v1/extraction-candidates/{candidate.id}/import"
    )
    second_import = await client.post(
        f"/api/v1/extraction-candidates/{candidate.id}/import"
    )

    assert fetched.status_code == 200
    assert "original_payload" in fetched.json()
    assert first_import.status_code == 201
    assert second_import.status_code == 200
    assert second_import.json()["profile_version_id"] == first_import.json()[
        "profile_version_id"
    ]


async def test_candidate_patch_returns_refreshed_candidate(client, db_session):
    candidate = await make_candidate(db_session)
    await db_session.commit()

    response = await client.patch(
        f"/api/v1/extraction-candidates/{candidate.id}",
        json={"revision": 1, "payload": candidate_payload()},
    )

    assert response.status_code == 200
    assert response.json()["revision"] == 2
    assert response.json()["updated_at"]


async def _make_document(db_session):
    document = SourceDocument(
        original_file_name="manual.pdf",
        media_type="application/pdf",
        file_size_bytes=10,
        file_hash_sha256="b" * 64,
        storage_uri="original/manual.pdf",
        validation_status="validated",
    )
    db_session.add(document)
    await db_session.commit()
    return document.id


async def _add_run(db_session, document_id, status):
    run = ProfileExtractionRun(
        source_document_id=document_id,
        status=status,
        extraction_provider="fake",
        extraction_model="extract-v1",
        embedding_provider="fake",
        embedding_model="embed-v1",
    )
    db_session.add(run)
    await db_session.commit()
    return run.id


async def test_queue_extraction_is_idempotent_while_run_unfinished(
    client, db_session, monkeypatch
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document_id = await _make_document(db_session)
    url = f"/api/v1/documents/{document_id}/extraction-runs"

    first = await client.post(url)
    second = await client.post(url)

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert dispatched == [first.json()["id"]]
    count = await db_session.scalar(select(func.count(ProfileExtractionRun.id)))
    assert count == 1


@pytest.mark.parametrize(
    "status",
    ["queued", "parsing", "retry_scheduled", "awaiting_review", "importing"],
)
async def test_queue_extraction_returns_existing_unfinished_run(
    client, db_session, monkeypatch, status
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document_id = await _make_document(db_session)
    run_id = await _add_run(db_session, document_id, status)

    response = await client.post(f"/api/v1/documents/{document_id}/extraction-runs")

    assert response.status_code == 200
    assert response.json()["id"] == run_id
    assert dispatched == []


@pytest.mark.parametrize("status", ["completed", "failed", "canceled"])
async def test_queue_extraction_creates_new_run_after_run_finished(
    client, db_session, monkeypatch, status
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document_id = await _make_document(db_session)
    old_id = await _add_run(db_session, document_id, status)

    response = await client.post(f"/api/v1/documents/{document_id}/extraction-runs")

    assert response.status_code == 202
    assert response.json()["id"] != old_id
    assert dispatched == [response.json()["id"]]


async def test_queue_extraction_only_checks_the_same_document(
    client, db_session, monkeypatch
):
    monkeypatch.setattr(document_router, "dispatch_extraction_task", lambda _: None)
    first_document = await _make_document(db_session)
    await _add_run(db_session, first_document, "queued")
    second_document = await _make_document(db_session)

    response = await client.post(f"/api/v1/documents/{second_document}/extraction-runs")

    assert response.status_code == 202


async def test_concurrent_queue_extraction_creates_one_run(
    client, db_session, monkeypatch
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document_id = await _make_document(db_session)
    url = f"/api/v1/documents/{document_id}/extraction-runs"

    responses = await asyncio.gather(*(client.post(url) for _ in range(8)))

    assert sorted(r.status_code for r in responses) == [200] * 7 + [202]
    assert len({r.json()["id"] for r in responses}) == 1
    assert len(dispatched) == 1
    count = await db_session.scalar(select(func.count(ProfileExtractionRun.id)))
    assert count == 1


async def test_retry_still_creates_new_run_for_failed_run(
    client, db_session, monkeypatch
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document_id = await _make_document(db_session)
    failed_id = await _add_run(db_session, document_id, "failed")

    response = await client.post(f"/api/v1/extraction-runs/{failed_id}/retry")

    assert response.status_code == 202
    assert response.json()["retry_of_run_id"] == failed_id
    assert dispatched == [response.json()["id"]]


async def test_retry_rejects_run_waiting_for_automatic_retry(
    client, db_session, monkeypatch
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document_id = await _make_document(db_session)
    run_id = await _add_run(db_session, document_id, "retry_scheduled")

    response = await client.post(f"/api/v1/extraction-runs/{run_id}/retry")

    assert response.status_code == 409
    assert dispatched == []
    count = await db_session.scalar(select(func.count(ProfileExtractionRun.id)))
    assert count == 1
