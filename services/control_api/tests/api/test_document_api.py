import asyncio
from io import BytesIO

from pypdf import PdfWriter
from sqlalchemy import func, select

from app.modules.document import admin_router
from app.modules.document import router as document_router
from app.modules.document.models import ProfileExtractionRun
from tests.api.test_admin_review import _admin_client
from tests.document.test_candidate_review import make_candidate
from tests.document.test_document_models import make_document, make_run
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


async def _document_id(db_session) -> int:
    document = make_document()
    db_session.add(document)
    await db_session.commit()
    return document.id


async def _run_count(db_session) -> int:
    return await db_session.scalar(
        select(func.count()).select_from(ProfileExtractionRun)
    )


async def test_queue_extraction_returns_existing_unfinished_run(
    client, db_session, monkeypatch
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document_id = await _document_id(db_session)
    url = f"/api/v1/documents/{document_id}/extraction-runs"

    first = await client.post(url)
    second = await client.post(url)

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert dispatched == [first.json()["id"]]
    assert await _run_count(db_session) == 1


async def test_queue_extraction_reuses_every_unfinished_status(
    client, db_session, monkeypatch
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document_id = await _document_id(db_session)
    run = make_run(document_id, status="retry_scheduled")
    db_session.add(run)
    await db_session.commit()

    response = await client.post(f"/api/v1/documents/{document_id}/extraction-runs")

    assert response.status_code == 200
    assert response.json()["id"] == run.id
    assert dispatched == []


async def test_queue_extraction_creates_new_run_after_run_ends(
    client, db_session, monkeypatch
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document_id = await _document_id(db_session)
    for status in ("completed", "failed", "canceled"):
        db_session.add(make_run(document_id, status=status))
    await db_session.commit()

    response = await client.post(f"/api/v1/documents/{document_id}/extraction-runs")

    assert response.status_code == 202
    assert dispatched == [response.json()["id"]]
    assert await _run_count(db_session) == 4


async def test_concurrent_queue_extraction_creates_one_run(
    client, db_session, monkeypatch
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document_id = await _document_id(db_session)

    responses = await asyncio.gather(
        *(
            client.post(f"/api/v1/documents/{document_id}/extraction-runs")
            for _ in range(5)
        )
    )

    assert sorted(response.status_code for response in responses) == [
        200,
        200,
        200,
        200,
        202,
    ]
    assert len({response.json()["id"] for response in responses}) == 1
    assert len(dispatched) == 1
    assert await _run_count(db_session) == 1


async def test_queue_extraction_unknown_document_is_404(client):
    response = await client.post("/api/v1/documents/999/extraction-runs")

    assert response.status_code == 404


async def test_admin_extract_button_is_idempotent(db_session, monkeypatch):
    dispatched = []
    monkeypatch.setattr(admin_router, "dispatch_extraction_task", dispatched.append)
    document_id = await _document_id(db_session)

    async with await _admin_client(monkeypatch) as admin:
        responses = await asyncio.gather(
            *(admin.post(f"/admin/documents/{document_id}/extract") for _ in range(3))
        )

    assert {response.status_code for response in responses} == {303}
    assert len(dispatched) == 1
    assert await _run_count(db_session) == 1


async def test_retry_scheduled_run_cannot_be_retried_manually(
    client, db_session, monkeypatch
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document_id = await _document_id(db_session)
    run = make_run(document_id, status="retry_scheduled")
    db_session.add(run)
    await db_session.commit()

    response = await client.post(f"/api/v1/extraction-runs/{run.id}/retry")

    assert response.status_code == 409
    assert dispatched == []
    assert await _run_count(db_session) == 1


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
