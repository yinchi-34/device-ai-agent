import asyncio

import pytest
from sqlalchemy import func, select

from app.modules.document import admin_router
from app.modules.document import router as document_router
from app.modules.document.models import ProfileExtractionRun
from tests.api.test_admin_review import _admin_client
from tests.document.test_document_models import make_document as new_document


@pytest.fixture
def dispatched(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", calls.append)
    monkeypatch.setattr(admin_router, "dispatch_extraction_task", calls.append)
    return calls


async def make_document(db_session):
    document = new_document()
    db_session.add(document)
    await db_session.flush()
    return document


async def count_runs(db_session) -> int:
    return await db_session.scalar(select(func.count(ProfileExtractionRun.id)))


async def test_repeat_submit_returns_existing_run_without_dispatch(
    client, db_session, dispatched
):
    document = await make_document(db_session)
    await db_session.commit()
    url = f"/api/v1/documents/{document.id}/extraction-runs"

    first = await client.post(url)
    second = await client.post(url)

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert dispatched == [first.json()["id"]]
    assert await count_runs(db_session) == 1


@pytest.mark.parametrize(
    "status",
    ["queued", "validating", "parsing", "extracting", "retry_scheduled",
     "validating_output", "awaiting_review", "importing"],
)
async def test_unfinished_run_is_reused(client, db_session, dispatched, status):
    document = await make_document(db_session)
    await db_session.commit()
    first = await client.post(f"/api/v1/documents/{document.id}/extraction-runs")
    run = await db_session.get(ProfileExtractionRun, first.json()["id"])
    run.status = status
    await db_session.commit()

    again = await client.post(f"/api/v1/documents/{document.id}/extraction-runs")

    assert again.status_code == 200
    assert again.json()["id"] == first.json()["id"]
    assert len(dispatched) == 1


@pytest.mark.parametrize("status", ["completed", "failed", "canceled"])
async def test_finished_run_allows_a_new_run(client, db_session, dispatched, status):
    document = await make_document(db_session)
    await db_session.commit()
    url = f"/api/v1/documents/{document.id}/extraction-runs"
    first = await client.post(url)
    run = await db_session.get(ProfileExtractionRun, first.json()["id"])
    run.status = status
    await db_session.commit()

    again = await client.post(url)

    assert again.status_code == 202
    assert again.json()["id"] != first.json()["id"]
    assert len(dispatched) == 2


async def test_other_documents_do_not_share_runs(client, db_session, dispatched):
    first_doc = await make_document(db_session)
    second_doc = await make_document(db_session)
    await db_session.commit()

    first = await client.post(f"/api/v1/documents/{first_doc.id}/extraction-runs")
    second = await client.post(f"/api/v1/documents/{second_doc.id}/extraction-runs")

    assert first.status_code == second.status_code == 202
    assert len(dispatched) == 2


async def test_concurrent_submits_create_one_run(client, db_session, dispatched):
    document = await make_document(db_session)
    await db_session.commit()
    url = f"/api/v1/documents/{document.id}/extraction-runs"

    responses = await asyncio.gather(*(client.post(url) for _ in range(8)))

    assert sorted(r.status_code for r in responses) == [200] * 7 + [202]
    assert len({r.json()["id"] for r in responses}) == 1
    assert len(dispatched) == 1
    assert await count_runs(db_session) == 1


async def test_missing_document_is_404(client, dispatched):
    response = await client.post("/api/v1/documents/999/extraction-runs")

    assert response.status_code == 404
    assert dispatched == []


async def test_admin_start_button_double_click_queues_once(
    db_session, monkeypatch, dispatched
):
    document = await make_document(db_session)
    await db_session.commit()
    admin = await _admin_client(monkeypatch)
    async with admin:
        responses = await asyncio.gather(
            *(admin.post(f"/admin/documents/{document.id}/extract") for _ in range(3))
        )

    assert all(r.status_code == 303 for r in responses)
    assert sorted(r.headers["location"].split("message=")[1] for r in responses) == [
        "extraction-already-running",
        "extraction-already-running",
        "extraction-queued",
    ]
    assert len(dispatched) == 1
    assert await count_runs(db_session) == 1


async def test_retry_endpoint_still_creates_run_from_failed(
    client, db_session, dispatched
):
    document = await make_document(db_session)
    await db_session.commit()
    first = await client.post(f"/api/v1/documents/{document.id}/extraction-runs")
    run = await db_session.get(ProfileExtractionRun, first.json()["id"])
    run.status = "failed"
    await db_session.commit()

    retried = await client.post(f"/api/v1/extraction-runs/{run.id}/retry")

    assert retried.status_code == 202
    assert retried.json()["retry_of_run_id"] == run.id
    assert dispatched == [run.id, retried.json()["id"]]


async def test_retry_endpoint_rejects_run_waiting_for_auto_retry(
    client, db_session, dispatched
):
    document = await make_document(db_session)
    await db_session.commit()
    first = await client.post(f"/api/v1/documents/{document.id}/extraction-runs")
    run = await db_session.get(ProfileExtractionRun, first.json()["id"])
    run.status = "retry_scheduled"
    await db_session.commit()

    response = await client.post(f"/api/v1/extraction-runs/{run.id}/retry")

    assert response.status_code == 409
    assert dispatched == [run.id]
    assert await count_runs(db_session) == 1
