import asyncio
from io import BytesIO

import pytest
from fastapi import Response
from pypdf import PdfWriter
from sqlalchemy import func, select

from app.db.session import async_session_factory
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


async def make_document(db_session):
    document = SourceDocument(
        original_file_name="manual.pdf",
        media_type="application/pdf",
        file_size_bytes=10,
        file_hash_sha256="a" * 64,
        storage_uri="original/manual.pdf",
        validation_status="validated",
    )
    db_session.add(document)
    await db_session.commit()
    return document


async def run_count(db_session):
    return await db_session.scalar(select(func.count(ProfileExtractionRun.id)))


async def test_queue_extraction_returns_unfinished_run(
    client, db_session, monkeypatch
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document = await make_document(db_session)
    url = f"/api/v1/documents/{document.id}/extraction-runs"

    first = await client.post(url)
    second = await client.post(url)

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert dispatched == [first.json()["id"]]
    assert await run_count(db_session) == 1


@pytest.mark.parametrize(
    "status",
    ["queued", "parsing", "awaiting_review", "importing", "retry_scheduled"],
)
async def test_queue_extraction_reuses_every_unfinished_status(
    client, db_session, monkeypatch, status
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document = await make_document(db_session)
    run = ProfileExtractionRun(
        source_document_id=document.id,
        status=status,
        extraction_provider="fake",
        extraction_model="extract-v1",
        embedding_provider="fake",
        embedding_model="embed-v1",
    )
    db_session.add(run)
    await db_session.commit()

    response = await client.post(f"/api/v1/documents/{document.id}/extraction-runs")

    assert response.status_code == 200
    assert response.json()["id"] == run.id
    assert dispatched == []


@pytest.mark.parametrize("status", ["completed", "failed", "canceled"])
async def test_queue_extraction_starts_new_run_after_finished_run(
    client, db_session, monkeypatch, status
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document = await make_document(db_session)
    db_session.add(
        ProfileExtractionRun(
            source_document_id=document.id,
            status=status,
            extraction_provider="fake",
            extraction_model="extract-v1",
            embedding_provider="fake",
            embedding_model="embed-v1",
        )
    )
    await db_session.commit()

    response = await client.post(f"/api/v1/documents/{document.id}/extraction-runs")

    assert response.status_code == 202
    assert dispatched == [response.json()["id"]]
    assert await run_count(db_session) == 2


async def test_concurrent_queue_extraction_creates_one_run(
    db_session, monkeypatch
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document = await make_document(db_session)

    async def submit():
        async with async_session_factory() as session:
            return await document_router.queue_extraction(
                document.id, Response(), session
            )

    runs = await asyncio.gather(*(submit() for _ in range(5)))

    assert len({run.id for run in runs}) == 1
    assert dispatched == [runs[0].id]
    assert await run_count(db_session) == 1


async def test_retry_rejects_run_waiting_for_automatic_retry(client, db_session):
    document = await make_document(db_session)
    run = ProfileExtractionRun(
        source_document_id=document.id,
        status="retry_scheduled",
        extraction_provider="fake",
        extraction_model="extract-v1",
        embedding_provider="fake",
        embedding_model="embed-v1",
    )
    db_session.add(run)
    await db_session.commit()

    response = await client.post(f"/api/v1/extraction-runs/{run.id}/retry")

    assert response.status_code == 409
    assert response.json()["code"] == "extraction_run_not_retryable"
    assert await run_count(db_session) == 1
