import asyncio

from app.modules.document import router as document_router
from app.modules.document.extraction import execute_extraction_run
from app.modules.document.models import ProfileExtractionRun
from app.modules.document.providers import ProviderError
from app.modules.document.storage import FileSystemDocumentStorage
from tests.api.test_document_api import valid_pdf_bytes
from tests.document.test_extraction_pipeline import (
    EmbeddingResult,
    ExtractionResult,
    FakeEmbeddingProvider,
    FakeExtractionProvider,
    FakeVisionOcrProvider,
    candidate_payload,
    pipeline_setup,
)


async def upload(client, monkeypatch, tmp_path):
    monkeypatch.setenv("DEVICE_DOCUMENT_STORAGE_ROOT", str(tmp_path))
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    response = await client.post(
        "/api/v1/documents",
        files={"file": ("manual.pdf", valid_pdf_bytes(), "application/pdf")},
    )
    return response.json()["id"], dispatched


async def test_repeat_submit_returns_existing_run(client, monkeypatch, tmp_path):
    document_id, dispatched = await upload(client, monkeypatch, tmp_path)
    url = f"/api/v1/documents/{document_id}/extraction-runs"

    first = await client.post(url)
    second = await client.post(url)

    assert (first.status_code, second.status_code) == (202, 200)
    assert second.json()["id"] == first.json()["id"]
    assert dispatched == [first.json()["id"]]


async def test_concurrent_submits_create_one_run(client, monkeypatch, tmp_path):
    document_id, dispatched = await upload(client, monkeypatch, tmp_path)
    url = f"/api/v1/documents/{document_id}/extraction-runs"

    responses = await asyncio.gather(*[client.post(url) for _ in range(6)])

    assert sorted(r.status_code for r in responses) == [200] * 5 + [202]
    assert len({r.json()["id"] for r in responses}) == 1
    assert len(dispatched) == 1


async def test_new_run_allowed_after_run_ends(client, db_session, monkeypatch, tmp_path):
    document_id, dispatched = await upload(client, monkeypatch, tmp_path)
    url = f"/api/v1/documents/{document_id}/extraction-runs"
    first = await client.post(url)
    run = await db_session.get(ProfileExtractionRun, first.json()["id"])
    run.status = "failed"
    await db_session.commit()

    second = await client.post(url)

    assert second.status_code == 202
    assert second.json()["id"] != first.json()["id"]
    assert len(dispatched) == 2


async def run_with_failures(db_session, tmp_path, failures, retries_used):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    # The agent retries each group internally, so one failure would be absorbed.
    extraction.failures = failures * 20
    await db_session.commit()
    try:
        await execute_extraction_run(
            db_session,
            run.id,
            storage=FileSystemDocumentStorage(tmp_path),
            extraction_provider=extraction,
            vision_provider=FakeVisionOcrProvider([]),
            embedding_provider=embedding,
            auto_retries_used=retries_used,
        )
    except ProviderError:
        pass
    await db_session.refresh(run)
    return run


async def test_retryable_error_schedules_retry(db_session, tmp_path):
    run = await run_with_failures(
        db_session, tmp_path, [ProviderError("provider_rate_limited", retryable=True)], 0
    )

    assert run.status == "retry_scheduled"
    assert run.error_code == "provider_rate_limited"
    assert run.error_detail["retry_count"] == 1
    assert run.error_detail["max_retries"] == 3
    assert run.finished_at is None


async def test_retries_exhausted_fails_with_count_3(db_session, tmp_path):
    run = await run_with_failures(
        db_session, tmp_path, [ProviderError("provider_rate_limited", retryable=True)], 3
    )

    assert run.status == "failed"
    assert run.error_detail["retry_count"] == 3
    assert run.error_detail["max_retries"] == 3


async def test_non_retryable_error_fails_immediately(db_session, tmp_path):
    run = await run_with_failures(
        db_session, tmp_path, [ProviderError("provider_auth", retryable=False)], 0
    )

    assert run.status == "failed"
    assert "retry_count" not in run.error_detail


async def test_success_after_scheduled_retry_completes(db_session, tmp_path):
    run = await run_with_failures(
        db_session, tmp_path, [ProviderError("provider_rate_limited", retryable=True)], 0
    )
    assert run.status == "retry_scheduled"
    run_id = run.id

    again = await execute_extraction_run(
        db_session,
        run_id,
        storage=FileSystemDocumentStorage(tmp_path),
        extraction_provider=FakeExtractionProvider(
            ExtractionResult(candidate_payload(), {"id": "response"}, {})
        ),
        vision_provider=FakeVisionOcrProvider([]),
        embedding_provider=FakeEmbeddingProvider(
            EmbeddingResult([[1.0, 0.0, 0.0]], "fake", "embed-v1", 3, {})
        ),
        auto_retries_used=1,
    )

    assert again.status == "awaiting_review"
    assert again.error_code is None


async def test_cannot_manually_retry_retry_scheduled_run(
    client, db_session, monkeypatch, tmp_path
):
    document_id, dispatched = await upload(client, monkeypatch, tmp_path)
    queued = await client.post(f"/api/v1/documents/{document_id}/extraction-runs")
    run = await db_session.get(ProfileExtractionRun, queued.json()["id"])
    run.status = "retry_scheduled"
    await db_session.commit()

    response = await client.post(f"/api/v1/extraction-runs/{run.id}/retry")

    assert response.status_code == 409
    assert len(dispatched) == 1
