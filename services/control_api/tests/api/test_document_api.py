import asyncio
from io import BytesIO

import pytest
from pypdf import PdfWriter
from sqlalchemy import select

from app.modules.document import admin_router
from app.modules.document import router as document_router
from app.modules.document.models import ProfileExtractionRun, SourceDocument
from app.modules.document.service import create_extraction_run
from tests.api.test_admin_review import _admin_client
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
        file_size_bytes=1,
        file_hash_sha256="0" * 64,
        storage_uri="original/manual.pdf",
    )
    db_session.add(document)
    await db_session.commit()
    return document


async def _add_run(db_session, document, status):
    run = await create_extraction_run(
        db_session,
        document.id,
        extraction_provider="fake",
        extraction_model="m",
        embedding_provider="fake",
        embedding_model="e",
    )
    run.status = status
    await db_session.commit()
    return run


async def test_queue_extraction_returns_unfinished_run_without_dispatch(
    client, db_session, monkeypatch
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document = await _make_document(db_session)
    url = f"/api/v1/documents/{document.id}/extraction-runs"

    first = await client.post(url)
    second = await client.post(url)

    assert (first.status_code, second.status_code) == (202, 200)
    assert second.json()["id"] == first.json()["id"]
    assert dispatched == [first.json()["id"]]


@pytest.mark.parametrize("status", ["completed", "failed", "canceled"])
async def test_queue_extraction_creates_new_run_after_finished_run(
    client, db_session, monkeypatch, status
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document = await _make_document(db_session)
    old = await _add_run(db_session, document, status)

    response = await client.post(f"/api/v1/documents/{document.id}/extraction-runs")

    assert response.status_code == 202
    assert response.json()["id"] != old.id
    assert dispatched == [response.json()["id"]]


@pytest.mark.parametrize("status", ["awaiting_review", "retry_scheduled"])
async def test_queue_extraction_reuses_run_in_other_unfinished_states(
    client, db_session, monkeypatch, status
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document = await _make_document(db_session)
    old = await _add_run(db_session, document, status)

    response = await client.post(f"/api/v1/documents/{document.id}/extraction-runs")

    assert response.status_code == 200
    assert response.json()["id"] == old.id
    assert dispatched == []


async def test_concurrent_queue_extraction_creates_single_run(
    client, db_session, monkeypatch
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document = await _make_document(db_session)

    responses = await asyncio.gather(
        *(
            client.post(f"/api/v1/documents/{document.id}/extraction-runs")
            for _ in range(5)
        )
    )

    runs = (await db_session.scalars(select(ProfileExtractionRun))).all()
    assert sorted(r.status_code for r in responses) == [200] * 4 + [202]
    assert len(runs) == 1
    assert dispatched == [runs[0].id]


async def test_admin_extract_button_is_idempotent(db_session, monkeypatch):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    monkeypatch.setattr(admin_router, "dispatch_extraction_task", dispatched.append)
    document = await _make_document(db_session)

    async with await _admin_client(monkeypatch) as admin:
        responses = await asyncio.gather(
            *(admin.post(f"/admin/documents/{document.id}/extract") for _ in range(3))
        )

    runs = (await db_session.scalars(select(ProfileExtractionRun))).all()
    assert [r.status_code for r in responses] == [303] * 3
    assert len(runs) == 1
    assert dispatched == [runs[0].id]


async def test_retry_rejects_retry_scheduled_run(client, db_session):
    document = await _make_document(db_session)
    run = await _add_run(db_session, document, "retry_scheduled")

    response = await client.post(f"/api/v1/extraction-runs/{run.id}/retry")

    assert response.status_code == 409
