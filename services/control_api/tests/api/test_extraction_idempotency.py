import asyncio

from sqlalchemy import func, select

from app.db.session import async_session_factory
from app.modules.document import admin_router
from app.modules.document import router as document_router
from app.modules.document.models import ProfileExtractionRun, SourceDocument
from tests.api.test_admin_review import _admin_client


async def _make_document(db_session) -> int:
    document = SourceDocument(
        original_file_name="manual.pdf",
        media_type="application/pdf",
        file_size_bytes=10,
        file_hash_sha256="f" * 64,
        storage_uri="original/manual.pdf",
        validation_status="validated",
    )
    db_session.add(document)
    await db_session.commit()
    return document.id


async def _run_count() -> int:
    async with async_session_factory() as session:
        return await session.scalar(select(func.count(ProfileExtractionRun.id)))


async def test_repeated_submit_returns_same_unfinished_run(
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
    assert await _run_count() == 1


async def test_submit_reuses_run_in_every_unfinished_status(
    client, db_session, monkeypatch
):
    monkeypatch.setattr(document_router, "dispatch_extraction_task", lambda _id: None)
    document_id = await _make_document(db_session)
    url = f"/api/v1/documents/{document_id}/extraction-runs"
    run_id = (await client.post(url)).json()["id"]

    for status in ("parsing", "retry_scheduled", "awaiting_review", "importing"):
        run = await db_session.get(ProfileExtractionRun, run_id)
        run.status = status
        await db_session.commit()
        response = await client.post(url)
        assert response.status_code == 200, status
        assert response.json()["id"] == run_id


async def test_submit_creates_new_run_after_run_finished(
    client, db_session, monkeypatch
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document_id = await _make_document(db_session)
    url = f"/api/v1/documents/{document_id}/extraction-runs"
    previous_id = (await client.post(url)).json()["id"]

    for status in ("failed", "canceled", "completed"):
        run = await db_session.get(ProfileExtractionRun, previous_id)
        run.status = status
        await db_session.commit()
        response = await client.post(url)
        assert response.status_code == 202, status
        assert response.json()["id"] != previous_id
        previous_id = response.json()["id"]

    assert len(dispatched) == 4


async def test_concurrent_submits_create_one_run(client, db_session, monkeypatch):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document_id = await _make_document(db_session)
    url = f"/api/v1/documents/{document_id}/extraction-runs"

    responses = await asyncio.gather(*(client.post(url) for _ in range(8)))

    assert sorted(r.status_code for r in responses) == [200] * 7 + [202]
    assert len({r.json()["id"] for r in responses}) == 1
    assert len(dispatched) == 1
    assert await _run_count() == 1


async def test_submit_unknown_document_is_404(client):
    response = await client.post("/api/v1/documents/999/extraction-runs")

    assert response.status_code == 404


async def test_manual_retry_of_failed_run_still_creates_new_run(
    client, db_session, monkeypatch
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document_id = await _make_document(db_session)
    run_id = (await client.post(f"/api/v1/documents/{document_id}/extraction-runs")).json()["id"]
    run = await db_session.get(ProfileExtractionRun, run_id)
    run.status = "failed"
    await db_session.commit()

    response = await client.post(f"/api/v1/extraction-runs/{run_id}/retry")

    assert response.status_code == 202
    assert response.json()["retry_of_run_id"] == run_id
    assert dispatched == [run_id, response.json()["id"]]


async def test_manual_retry_of_retry_scheduled_run_is_conflict(
    client, db_session, monkeypatch
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document_id = await _make_document(db_session)
    run_id = (await client.post(f"/api/v1/documents/{document_id}/extraction-runs")).json()["id"]
    run = await db_session.get(ProfileExtractionRun, run_id)
    run.status = "retry_scheduled"
    await db_session.commit()

    response = await client.post(f"/api/v1/extraction-runs/{run_id}/retry")

    assert response.status_code == 409
    assert response.json()["code"] == "extraction_run_not_retryable"
    assert dispatched == [run_id]
    assert await _run_count() == 1


async def test_admin_start_button_is_idempotent(db_session, monkeypatch):
    dispatched = []
    monkeypatch.setattr(admin_router, "dispatch_extraction_task", dispatched.append)
    document_id = await _make_document(db_session)

    async with await _admin_client(monkeypatch) as client:
        form = f"/admin/documents/{document_id}/extract"
        token = client.cookies.get("admin_csrf")
        responses = await asyncio.gather(
            *(client.post(form, data={"_csrf_token": token}) for _ in range(4))
        )

    assert {r.status_code for r in responses} == {303}
    assert len(dispatched) == 1
    assert await _run_count() == 1
