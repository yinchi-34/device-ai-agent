from io import BytesIO

from pypdf import PdfWriter

from app.modules.document import router as document_router
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
    from app.modules.document.models import SourceDocument

    document = SourceDocument(
        original_file_name="manual.pdf",
        media_type="application/pdf",
        file_size_bytes=10,
        file_hash_sha256="e" * 64,
        storage_uri="original/manual.pdf",
        validation_status="validated",
    )
    db_session.add(document)
    await db_session.commit()
    return document.id


async def test_queue_extraction_is_idempotent_while_run_unfinished(
    client, db_session, monkeypatch
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document_id = await _make_document(db_session)
    url = f"/api/v1/documents/{document_id}/extraction-runs"

    first = await client.post(url)
    second = await client.post(url)

    assert (first.status_code, second.status_code) == (202, 200)
    assert second.json()["id"] == first.json()["id"]
    assert dispatched == [first.json()["id"]]


async def test_queue_extraction_reuses_retry_scheduled_run(
    client, db_session, monkeypatch
):
    from app.modules.document.models import ProfileExtractionRun

    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document_id = await _make_document(db_session)
    first = await client.post(f"/api/v1/documents/{document_id}/extraction-runs")
    run = await db_session.get(ProfileExtractionRun, first.json()["id"])
    run.status = "retry_scheduled"
    await db_session.commit()

    again = await client.post(f"/api/v1/documents/{document_id}/extraction-runs")

    assert again.status_code == 200
    assert again.json()["id"] == first.json()["id"]
    assert len(dispatched) == 1


async def test_queue_extraction_starts_new_run_after_run_ends(
    client, db_session, monkeypatch
):
    from app.modules.document.models import ProfileExtractionRun

    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document_id = await _make_document(db_session)
    url = f"/api/v1/documents/{document_id}/extraction-runs"
    for ended in ("failed", "canceled", "completed"):
        queued = await client.post(url)
        assert queued.status_code == 202
        run = await db_session.get(ProfileExtractionRun, queued.json()["id"])
        run.status = ended
        await db_session.commit()

    assert len(dispatched) == 3


async def test_concurrent_queue_extraction_creates_one_run(
    client, db_session, monkeypatch
):
    import asyncio

    from sqlalchemy import func, select

    from app.modules.document.models import ProfileExtractionRun

    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document_id = await _make_document(db_session)
    url = f"/api/v1/documents/{document_id}/extraction-runs"

    responses = await asyncio.gather(*(client.post(url) for _ in range(6)))

    assert sorted(r.status_code for r in responses) == [200] * 5 + [202]
    assert len({r.json()["id"] for r in responses}) == 1
    assert len(dispatched) == 1
    assert await db_session.scalar(select(func.count(ProfileExtractionRun.id))) == 1


async def test_retry_endpoint_rejects_retry_scheduled_run(
    client, db_session, monkeypatch
):
    from app.modules.document.models import ProfileExtractionRun

    monkeypatch.setattr(document_router, "dispatch_extraction_task", lambda _id: None)
    document_id = await _make_document(db_session)
    queued = await client.post(f"/api/v1/documents/{document_id}/extraction-runs")
    run = await db_session.get(ProfileExtractionRun, queued.json()["id"])
    run.status = "retry_scheduled"
    await db_session.commit()

    response = await client.post(f"/api/v1/extraction-runs/{run.id}/retry")

    assert response.status_code == 409
    assert response.json()["code"] == "extraction_run_not_retryable"
