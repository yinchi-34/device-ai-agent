import pytest

from app.core.errors import DomainError
from app.modules.document.extraction import MAX_AUTO_RETRIES, execute_extraction_run
from app.modules.document.providers import FakeVisionOcrProvider, ProviderError
from app.modules.document.service import retry_extraction_run
from app.modules.document.storage import FileSystemDocumentStorage
from tests.document.test_extraction_pipeline import pipeline_setup

# provider_timeout/connection errors are retried inside extraction first.
RETRYABLE_CODE = "provider_rate_limited"


async def _execute(db_session, tmp_path, run, extraction, embedding, retries):
    return await execute_extraction_run(
        db_session,
        run.id,
        storage=FileSystemDocumentStorage(tmp_path),
        extraction_provider=extraction,
        vision_provider=FakeVisionOcrProvider([]),
        embedding_provider=embedding,
        retries=retries,
    )


@pytest.mark.parametrize("retries", [0, 1, 2])
async def test_retryable_error_schedules_retry(db_session, tmp_path, retries):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [ProviderError(RETRYABLE_CODE, retryable=True)]

    with pytest.raises(ProviderError):
        await _execute(db_session, tmp_path, run, extraction, embedding, retries)

    await db_session.refresh(run)
    assert run.status == "retry_scheduled"
    assert run.error_code == RETRYABLE_CODE
    assert run.error_detail["retry_count"] == retries + 1
    assert run.error_detail["max_retries"] == 3
    assert run.finished_at is None


async def test_retryable_error_fails_after_retries_exhausted(db_session, tmp_path):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [ProviderError(RETRYABLE_CODE, retryable=True)]

    with pytest.raises(ProviderError):
        await _execute(
            db_session, tmp_path, run, extraction, embedding, MAX_AUTO_RETRIES
        )

    await db_session.refresh(run)
    assert run.status == "failed"
    assert run.error_code == RETRYABLE_CODE
    assert run.error_detail["retry_count"] == 3
    assert run.error_detail["max_retries"] == 3
    assert run.finished_at is not None


async def test_non_retryable_error_fails_immediately(db_session, tmp_path):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [ProviderError("provider_auth", retryable=False)]

    with pytest.raises(ProviderError):
        await _execute(db_session, tmp_path, run, extraction, embedding, 0)

    await db_session.refresh(run)
    assert run.status == "failed"
    assert run.error_detail == {"retryable": False}


async def test_success_after_scheduled_retry_completes(db_session, tmp_path):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [ProviderError(RETRYABLE_CODE, retryable=True)]
    with pytest.raises(ProviderError):
        await _execute(db_session, tmp_path, run, extraction, embedding, 0)

    await _execute(db_session, tmp_path, run, extraction, embedding, 1)

    await db_session.refresh(run)
    assert run.status == "awaiting_review"
    assert run.error_code is None
    assert run.error_detail == {}


async def test_retry_scheduled_run_cannot_be_retried_manually(db_session, tmp_path):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [ProviderError(RETRYABLE_CODE, retryable=True)]
    with pytest.raises(ProviderError):
        await _execute(db_session, tmp_path, run, extraction, embedding, 0)

    with pytest.raises(DomainError) as caught:
        await retry_extraction_run(db_session, run.id)

    assert caught.value.status_code == 409


async def test_retry_endpoint_returns_409_for_retry_scheduled(
    client, db_session, tmp_path
):
    run, _, _ = await pipeline_setup(db_session, tmp_path)
    run.status = "retry_scheduled"
    await db_session.commit()

    response = await client.post(f"/api/v1/extraction-runs/{run.id}/retry")

    assert response.status_code == 409
    assert response.json()["code"] == "extraction_run_not_retryable"
