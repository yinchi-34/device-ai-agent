import asyncio

from app.modules.document import admin_router
from app.modules.document import router as document_router
from app.modules.document.models import ProfileExtractionRun, SourceDocument
from tests.api.test_admin_review import _admin_client


async def make_document(db_session) -> int:
    document = SourceDocument(
        original_file_name="manual.pdf",
        media_type="application/pdf",
        file_size_bytes=1,
        file_hash_sha256="a" * 64,
        storage_uri="original/manual.pdf",
        validation_status="validated",
    )
    db_session.add(document)
    await db_session.commit()
    return document.id


async def count_runs(db_session, document_id: int) -> int:
    runs = await db_session.scalars(
        ProfileExtractionRun.__table__.select().where(
            ProfileExtractionRun.source_document_id == document_id
        )
    )
    return len(runs.all())


async def test_second_submit_returns_active_run(client, db_session, monkeypatch):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document_id = await make_document(db_session)
    url = f"/api/v1/documents/{document_id}/extraction-runs"

    first = await client.post(url)
    second = await client.post(url)

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert dispatched == [first.json()["id"]]
    assert await count_runs(db_session, document_id) == 1


async def test_submit_reuses_run_in_any_unfinished_status(
    client, db_session, monkeypatch
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document_id = await make_document(db_session)
    url = f"/api/v1/documents/{document_id}/extraction-runs"
    run_id = (await client.post(url)).json()["id"]

    for status in ("retry_scheduled", "extracting", "awaiting_review"):
        run = await db_session.get(ProfileExtractionRun, run_id)
        run.status = status
        await db_session.commit()
        response = await client.post(url)
        assert response.status_code == 200
        assert response.json()["id"] == run_id

    assert dispatched == [run_id]


async def test_submit_creates_new_run_after_terminal_status(
    client, db_session, monkeypatch
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document_id = await make_document(db_session)
    url = f"/api/v1/documents/{document_id}/extraction-runs"
    previous_id = (await client.post(url)).json()["id"]

    for status in ("failed", "completed", "canceled"):
        run = await db_session.get(ProfileExtractionRun, previous_id)
        run.status = status
        await db_session.commit()
        response = await client.post(url)
        assert response.status_code == 202
        assert response.json()["id"] != previous_id
        previous_id = response.json()["id"]

    assert len(dispatched) == 4


async def test_concurrent_submits_create_single_run(client, db_session, monkeypatch):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document_id = await make_document(db_session)
    url = f"/api/v1/documents/{document_id}/extraction-runs"

    responses = await asyncio.gather(*(client.post(url) for _ in range(8)))

    assert sorted(r.status_code for r in responses) == [200] * 7 + [202]
    assert len({r.json()["id"] for r in responses}) == 1
    assert len(dispatched) == 1
    assert await count_runs(db_session, document_id) == 1


async def test_submit_unknown_document_is_404(client):
    response = await client.post("/api/v1/documents/9999/extraction-runs")

    assert response.status_code == 404


async def test_admin_start_extraction_is_idempotent(db_session, monkeypatch):
    dispatched = []
    monkeypatch.setattr(admin_router, "dispatch_extraction_task", dispatched.append)
    document_id = await make_document(db_session)
    admin = await _admin_client(monkeypatch)

    async with admin:
        responses = await asyncio.gather(
            *(admin.post(f"/admin/documents/{document_id}/extract") for _ in range(4))
        )

    assert all(r.status_code == 303 for r in responses)
    assert len(dispatched) == 1
    assert await count_runs(db_session, document_id) == 1


async def test_retry_still_requires_failed_and_works_for_failed(
    client, db_session, monkeypatch
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document_id = await make_document(db_session)
    run_id = (
        await client.post(f"/api/v1/documents/{document_id}/extraction-runs")
    ).json()["id"]

    run = await db_session.get(ProfileExtractionRun, run_id)
    run.status = "failed"
    await db_session.commit()
    retried = await client.post(f"/api/v1/extraction-runs/{run_id}/retry")

    assert retried.status_code == 202
    assert retried.json()["attempt_number"] == 2
    assert dispatched == [run_id, retried.json()["id"]]


async def test_retry_scheduled_run_cannot_be_retried_manually(
    client, db_session, monkeypatch
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document_id = await make_document(db_session)
    run_id = (
        await client.post(f"/api/v1/documents/{document_id}/extraction-runs")
    ).json()["id"]
    run = await db_session.get(ProfileExtractionRun, run_id)
    run.status = "retry_scheduled"
    await db_session.commit()

    response = await client.post(f"/api/v1/extraction-runs/{run_id}/retry")

    assert response.status_code == 409
    assert dispatched == [run_id]
    assert await count_runs(db_session, document_id) == 1
