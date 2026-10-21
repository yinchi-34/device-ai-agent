import asyncio

import pytest
from sqlalchemy import func, select

from app.modules.document import admin_router
from app.modules.document import router as document_router
from app.modules.document.extraction import execute_extraction_run
from app.modules.document.models import ProfileExtractionRun, SourceDocument
from app.modules.document.providers import ProviderError
from app.modules.document.storage import FileSystemDocumentStorage
from tests.api.test_admin_review import _admin_client
from tests.document.test_extraction_pipeline import (
    FakeVisionOcrProvider,
    pipeline_setup,
)


@pytest.fixture
def dispatched(monkeypatch):
    calls = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", calls.append)
    monkeypatch.setattr(admin_router, "dispatch_extraction_task", calls.append)
    return calls


async def make_document(db_session) -> int:
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
    return document.id


async def run_count(db_session) -> int:
    return await db_session.scalar(select(func.count(ProfileExtractionRun.id)))


async def test_repeat_submit_returns_existing_run(client, db_session, dispatched):
    document_id = await make_document(db_session)
    url = f"/api/v1/documents/{document_id}/extraction-runs"

    first = await client.post(url)
    second = await client.post(url)

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert dispatched == [first.json()["id"]]
    assert await run_count(db_session) == 1


@pytest.mark.parametrize("status", ["retry_scheduled", "extracting", "awaiting_review"])
async def test_unfinished_status_blocks_new_run(client, db_session, dispatched, status):
    document_id = await make_document(db_session)
    first = await client.post(f"/api/v1/documents/{document_id}/extraction-runs")
    await db_session.execute(
        ProfileExtractionRun.__table__.update().values(status=status)
    )
    await db_session.commit()

    again = await client.post(f"/api/v1/documents/{document_id}/extraction-runs")

    assert again.status_code == 200
    assert again.json()["id"] == first.json()["id"]
    assert len(dispatched) == 1


@pytest.mark.parametrize("status", ["completed", "failed", "canceled"])
async def test_finished_run_allows_new_run(client, db_session, dispatched, status):
    document_id = await make_document(db_session)
    url = f"/api/v1/documents/{document_id}/extraction-runs"
    first = await client.post(url)
    await db_session.execute(
        ProfileExtractionRun.__table__.update().values(status=status)
    )
    await db_session.commit()

    second = await client.post(url)

    assert second.status_code == 202
    assert second.json()["id"] != first.json()["id"]
    assert len(dispatched) == 2


async def test_concurrent_submits_create_one_run(client, db_session, dispatched):
    document_id = await make_document(db_session)
    url = f"/api/v1/documents/{document_id}/extraction-runs"

    responses = await asyncio.gather(*(client.post(url) for _ in range(5)))

    assert sorted(r.status_code for r in responses) == [200] * 4 + [202]
    assert len({r.json()["id"] for r in responses}) == 1
    assert len(dispatched) == 1
    assert await run_count(db_session) == 1


async def test_admin_button_double_click_dispatches_once(
    db_session, monkeypatch, dispatched
):
    document_id = await make_document(db_session)
    async with await _admin_client(monkeypatch) as admin:
        responses = [
            await admin.post(f"/admin/documents/{document_id}/extract")
            for _ in range(2)
        ]

    assert [r.status_code for r in responses] == [303, 303]
    assert len(dispatched) == 1
    assert await run_count(db_session) == 1


async def test_failed_run_retry_still_creates_new_run(client, db_session, dispatched):
    document_id = await make_document(db_session)
    first = await client.post(f"/api/v1/documents/{document_id}/extraction-runs")
    await db_session.execute(
        ProfileExtractionRun.__table__.update().values(status="failed")
    )
    await db_session.commit()

    retried = await client.post(f"/api/v1/extraction-runs/{first.json()['id']}/retry")

    assert retried.status_code == 202
    assert retried.json()["id"] != first.json()["id"]
    assert retried.json()["attempt_number"] == 2


async def test_retry_scheduled_run_cannot_be_manually_retried(
    client, db_session, dispatched
):
    document_id = await make_document(db_session)
    first = await client.post(f"/api/v1/documents/{document_id}/extraction-runs")
    await db_session.execute(
        ProfileExtractionRun.__table__.update().values(status="retry_scheduled")
    )
    await db_session.commit()

    response = await client.post(f"/api/v1/extraction-runs/{first.json()['id']}/retry")

    assert response.status_code == 409
    assert await run_count(db_session) == 1


class _Failing:
    def __init__(self, error):
        self.error = error
        self.call_count = 0

    async def extract(self, chunks, context):
        self.call_count += 1
        raise self.error


async def _execute(db_session, tmp_path, run, extraction, embedding, **kwargs):
    try:
        await execute_extraction_run(
            db_session,
            run.id,
            storage=FileSystemDocumentStorage(tmp_path),
            extraction_provider=extraction,
            vision_provider=FakeVisionOcrProvider([]),
            embedding_provider=embedding,
            **kwargs,
        )
    except ProviderError:
        pass
    await db_session.refresh(run)


@pytest.mark.parametrize("retries_done", [0, 1, 2])
async def test_retryable_error_schedules_retry(db_session, tmp_path, retries_done):
    run, _, embedding = await pipeline_setup(db_session, tmp_path)
    failing = _Failing(ProviderError("provider_rate_limited", retryable=True))

    await _execute(
        db_session, tmp_path, run, failing, embedding, retries_done=retries_done
    )

    assert run.status == "retry_scheduled"
    assert run.error_code == "provider_rate_limited"
    assert run.error_detail["retry_count"] == retries_done + 1
    assert run.error_detail["max_retries"] == 3
    assert run.finished_at is None


async def test_retryable_error_fails_after_retries_exhausted(db_session, tmp_path):
    run, _, embedding = await pipeline_setup(db_session, tmp_path)
    failing = _Failing(ProviderError("provider_rate_limited", retryable=True))

    await _execute(db_session, tmp_path, run, failing, embedding, retries_done=3)

    assert run.status == "failed"
    assert run.error_code == "provider_rate_limited"
    assert run.error_detail["retry_count"] == 3
    assert run.error_detail["max_retries"] == 3
    assert run.finished_at is not None


async def test_non_retryable_error_fails_immediately(db_session, tmp_path):
    run, _, embedding = await pipeline_setup(db_session, tmp_path)
    failing = _Failing(ProviderError("provider_bad_request", retryable=False))

    await _execute(db_session, tmp_path, run, failing, embedding)

    assert run.status == "failed"
    assert run.error_code == "provider_bad_request"
    assert "retry_count" not in run.error_detail


async def test_success_after_scheduled_retry_completes(db_session, tmp_path):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    failing = _Failing(ProviderError("provider_rate_limited", retryable=True))
    await _execute(db_session, tmp_path, run, failing, embedding)
    assert run.status == "retry_scheduled"

    await _execute(db_session, tmp_path, run, extraction, embedding, retries_done=1)

    assert run.status == "awaiting_review"
    assert run.error_code is None
    assert run.error_detail == {}
