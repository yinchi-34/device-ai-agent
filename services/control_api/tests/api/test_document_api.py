import asyncio
from io import BytesIO

import pytest
from pypdf import PdfWriter

from app.modules.document import router as document_router
from app.modules.document.models import ProfileExtractionRun
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


async def upload_document(client, monkeypatch, tmp_path):
    monkeypatch.setenv("DEVICE_DOCUMENT_STORAGE_ROOT", str(tmp_path))
    response = await client.post(
        "/api/v1/documents",
        files={"file": ("manual.pdf", valid_pdf_bytes(), "application/pdf")},
    )
    return response.json()["id"]


async def test_queue_extraction_is_idempotent_while_run_unfinished(
    client, monkeypatch, tmp_path
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document_id = await upload_document(client, monkeypatch, tmp_path)
    url = f"/api/v1/documents/{document_id}/extraction-runs"

    first = await client.post(url)
    second = await client.post(url)

    assert (first.status_code, second.status_code) == (202, 200)
    assert second.json()["id"] == first.json()["id"]
    assert dispatched == [first.json()["id"]]


async def test_queue_extraction_concurrent_submits_create_one_run(
    client, monkeypatch, tmp_path
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document_id = await upload_document(client, monkeypatch, tmp_path)
    url = f"/api/v1/documents/{document_id}/extraction-runs"

    responses = await asyncio.gather(*(client.post(url) for _ in range(5)))

    assert sorted(r.status_code for r in responses) == [200, 200, 200, 200, 202]
    assert len({r.json()["id"] for r in responses}) == 1
    assert len(dispatched) == 1


@pytest.mark.parametrize("status", ["completed", "failed", "canceled"])
async def test_queue_extraction_creates_new_run_after_terminal_run(
    client, db_session, monkeypatch, tmp_path, status
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document_id = await upload_document(client, monkeypatch, tmp_path)
    url = f"/api/v1/documents/{document_id}/extraction-runs"
    first = await client.post(url)
    run = await db_session.get(ProfileExtractionRun, first.json()["id"])
    run.status = status
    await db_session.commit()

    second = await client.post(url)

    assert second.status_code == 202
    assert second.json()["id"] != first.json()["id"]
    assert len(dispatched) == 2


@pytest.mark.parametrize("status", ["retry_scheduled", "queued", "awaiting_review"])
async def test_retry_endpoint_rejects_unfinished_run(
    client, db_session, monkeypatch, tmp_path, status
):
    monkeypatch.setattr(document_router, "dispatch_extraction_task", lambda _id: None)
    document_id = await upload_document(client, monkeypatch, tmp_path)
    first = await client.post(f"/api/v1/documents/{document_id}/extraction-runs")
    run = await db_session.get(ProfileExtractionRun, first.json()["id"])
    run.status = status
    await db_session.commit()

    response = await client.post(f"/api/v1/extraction-runs/{run.id}/retry")

    assert response.status_code == 409
