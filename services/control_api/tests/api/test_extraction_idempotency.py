import asyncio

from sqlalchemy import func, select

from app.modules.document import admin_router
from app.modules.document import router as document_router
from app.modules.document.models import ProfileExtractionRun
from tests.api.test_admin_review import _admin_client
from tests.api.test_document_api import valid_pdf_bytes


async def _upload(client, monkeypatch, tmp_path):
    monkeypatch.setenv("DEVICE_DOCUMENT_STORAGE_ROOT", str(tmp_path))
    response = await client.post(
        "/api/v1/documents",
        files={"file": ("manual.pdf", valid_pdf_bytes(), "application/pdf")},
    )
    assert response.status_code == 201
    return response.json()["id"]


async def _run_count(db_session) -> int:
    return await db_session.scalar(select(func.count(ProfileExtractionRun.id)))


async def test_second_submit_returns_existing_run(
    client, db_session, monkeypatch, tmp_path
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document_id = await _upload(client, monkeypatch, tmp_path)
    url = f"/api/v1/documents/{document_id}/extraction-runs"

    first = await client.post(url)
    second = await client.post(url)

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert dispatched == [first.json()["id"]]
    assert await _run_count(db_session) == 1


async def test_in_progress_statuses_are_returned_not_duplicated(
    client, db_session, monkeypatch, tmp_path
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document_id = await _upload(client, monkeypatch, tmp_path)
    url = f"/api/v1/documents/{document_id}/extraction-runs"
    run_id = (await client.post(url)).json()["id"]

    for status in ("parsing", "retry_scheduled", "awaiting_review"):
        run = await db_session.get(ProfileExtractionRun, run_id)
        run.status = status
        await db_session.commit()
        response = await client.post(url)
        assert response.status_code == 200
        assert response.json()["id"] == run_id

    assert dispatched == [run_id]


async def test_finished_run_allows_new_run(client, db_session, monkeypatch, tmp_path):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document_id = await _upload(client, monkeypatch, tmp_path)
    url = f"/api/v1/documents/{document_id}/extraction-runs"
    previous_ids = []

    for status in ("failed", "canceled", "completed"):
        response = await client.post(url)
        assert response.status_code == 202
        assert response.json()["id"] not in previous_ids
        previous_ids.append(response.json()["id"])
        run = await db_session.get(ProfileExtractionRun, response.json()["id"])
        run.status = status
        await db_session.commit()

    assert dispatched == previous_ids


async def test_runs_of_other_documents_do_not_block(
    client, monkeypatch, tmp_path
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    first_doc = await _upload(client, monkeypatch, tmp_path)
    second_doc = await _upload(client, monkeypatch, tmp_path)

    first = await client.post(f"/api/v1/documents/{first_doc}/extraction-runs")
    second = await client.post(f"/api/v1/documents/{second_doc}/extraction-runs")

    assert first.status_code == second.status_code == 202
    assert first.json()["id"] != second.json()["id"]
    assert len(dispatched) == 2


async def test_concurrent_submits_create_one_run(
    client, db_session, monkeypatch, tmp_path
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document_id = await _upload(client, monkeypatch, tmp_path)
    url = f"/api/v1/documents/{document_id}/extraction-runs"

    responses = await asyncio.gather(*(client.post(url) for _ in range(8)))

    assert sorted(response.status_code for response in responses) == [200] * 7 + [202]
    assert len({response.json()["id"] for response in responses}) == 1
    assert len(dispatched) == 1
    assert await _run_count(db_session) == 1


async def test_unknown_document_is_not_found(client, monkeypatch):
    monkeypatch.setattr(document_router, "dispatch_extraction_task", lambda _: None)

    response = await client.post("/api/v1/documents/999/extraction-runs")

    assert response.status_code == 404


async def test_retry_still_creates_run_for_failed_run(
    client, db_session, monkeypatch, tmp_path
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document_id = await _upload(client, monkeypatch, tmp_path)
    run_id = (
        await client.post(f"/api/v1/documents/{document_id}/extraction-runs")
    ).json()["id"]
    run = await db_session.get(ProfileExtractionRun, run_id)
    run.status = "failed"
    await db_session.commit()

    response = await client.post(f"/api/v1/extraction-runs/{run_id}/retry")

    assert response.status_code == 202
    assert response.json()["retry_of_run_id"] == run_id
    assert dispatched == [run_id, response.json()["id"]]


async def test_retry_scheduled_run_cannot_be_retried_manually(
    client, db_session, monkeypatch, tmp_path
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    document_id = await _upload(client, monkeypatch, tmp_path)
    run_id = (
        await client.post(f"/api/v1/documents/{document_id}/extraction-runs")
    ).json()["id"]
    run = await db_session.get(ProfileExtractionRun, run_id)
    run.status = "retry_scheduled"
    await db_session.commit()

    response = await client.post(f"/api/v1/extraction-runs/{run_id}/retry")

    assert response.status_code == 409
    assert dispatched == [run_id]
    assert await _run_count(db_session) == 1


async def test_admin_double_click_creates_one_run(
    client, db_session, monkeypatch, tmp_path
):
    dispatched = []
    monkeypatch.setattr(admin_router, "dispatch_extraction_task", dispatched.append)
    document_id = await _upload(client, monkeypatch, tmp_path)
    admin = await _admin_client(monkeypatch)

    async with admin:
        responses = await asyncio.gather(
            *(admin.post(f"/admin/documents/{document_id}/extract") for _ in range(3))
        )

    assert all(response.status_code == 303 for response in responses)
    locations = sorted(response.headers["location"] for response in responses)
    assert locations.count(
        f"/admin/documents/{document_id}?message=extraction-queued"
    ) == 1
    assert locations.count(
        f"/admin/documents/{document_id}?message=extraction-already-running"
    ) == 2
    assert len(dispatched) == 1
    assert await _run_count(db_session) == 1
