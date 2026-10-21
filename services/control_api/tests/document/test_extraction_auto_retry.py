import pytest

from app.modules.document.extraction import execute_extraction_run
from app.modules.document.providers import FakeVisionOcrProvider, ProviderError
from app.modules.document.storage import FileSystemDocumentStorage
from tests.document.test_extraction_pipeline import pipeline_setup


async def _execute(db_session, tmp_path, run, extraction, embedding, **kwargs):
    return await execute_extraction_run(
        db_session,
        run.id,
        storage=FileSystemDocumentStorage(tmp_path),
        extraction_provider=extraction,
        vision_provider=FakeVisionOcrProvider([]),
        embedding_provider=embedding,
        **kwargs,
    )


async def test_retryable_error_schedules_retry(db_session, tmp_path):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [ProviderError("provider_rate_limited", retryable=True)]

    with pytest.raises(ProviderError):
        await _execute(
            db_session, tmp_path, run, extraction, embedding,
            retry_count=0, max_retries=3,
        )

    await db_session.refresh(run)
    assert run.status == "retry_scheduled"
    assert run.error_code == "provider_rate_limited"
    assert run.error_detail["retry_count"] == 1
    assert run.error_detail["max_retries"] == 3
    assert run.finished_at is None


async def test_retry_count_grows_and_last_failure_is_failed(db_session, tmp_path):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)

    for retries in range(3):
        extraction.failures = [ProviderError("provider_unavailable", retryable=True)]
        with pytest.raises(ProviderError):
            await _execute(
                db_session, tmp_path, run, extraction, embedding,
                retry_count=retries, max_retries=3,
            )
        await db_session.refresh(run)
        assert run.status == "retry_scheduled"
        assert run.error_detail["retry_count"] == retries + 1

    extraction.failures = [ProviderError("provider_unavailable", retryable=True)]
    with pytest.raises(ProviderError):
        await _execute(
            db_session, tmp_path, run, extraction, embedding,
            retry_count=3, max_retries=3,
        )

    await db_session.refresh(run)
    assert run.status == "failed"
    assert run.error_code == "provider_unavailable"
    assert run.error_detail["retry_count"] == 3
    assert run.error_detail["max_retries"] == 3
    assert run.finished_at is not None


async def test_success_after_scheduled_retry_completes_normally(db_session, tmp_path):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [ProviderError("provider_unavailable", retryable=True)]
    with pytest.raises(ProviderError):
        await _execute(
            db_session, tmp_path, run, extraction, embedding,
            retry_count=0, max_retries=3,
        )

    await _execute(
        db_session, tmp_path, run, extraction, embedding,
        retry_count=1, max_retries=3,
    )

    await db_session.refresh(run)
    assert run.status == "awaiting_review"
    assert run.error_code is None
    assert run.error_detail == {}


async def test_non_retryable_error_fails_immediately(db_session, tmp_path):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [ProviderError("provider_auth_failed", retryable=False)]

    with pytest.raises(ProviderError):
        await _execute(
            db_session, tmp_path, run, extraction, embedding,
            retry_count=0, max_retries=3,
        )

    await db_session.refresh(run)
    assert run.status == "failed"
    assert run.error_code == "provider_auth_failed"
    assert "retry_count" not in run.error_detail
    assert run.finished_at is not None
