import pytest

from app.modules.document.extraction import execute_extraction_run
from app.modules.document.providers import FakeVisionOcrProvider, ProviderError
from app.modules.document.storage import FileSystemDocumentStorage
from tests.document.test_extraction_pipeline import pipeline_setup


async def run_attempt(db_session, tmp_path, run, extraction, embedding, retries):
    return await execute_extraction_run(
        db_session,
        run.id,
        storage=FileSystemDocumentStorage(tmp_path),
        extraction_provider=extraction,
        vision_provider=FakeVisionOcrProvider([]),
        embedding_provider=embedding,
        retries=retries,
    )


async def test_retryable_error_schedules_retry_instead_of_failing(
    db_session, tmp_path
):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [ProviderError("provider_rate_limited", retryable=True)]

    with pytest.raises(ProviderError):
        await run_attempt(db_session, tmp_path, run, extraction, embedding, 0)

    assert run.status == "retry_scheduled"
    assert run.error_code == "provider_rate_limited"
    assert run.error_detail["retry_count"] == 1
    assert run.error_detail["max_retries"] == 3
    assert run.finished_at is None


async def test_retry_count_grows_then_run_fails_after_three_retries(
    db_session, tmp_path
):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [
        ProviderError("provider_unavailable", retryable=True) for _ in range(4)
    ]

    for retries in range(3):
        with pytest.raises(ProviderError):
            await run_attempt(db_session, tmp_path, run, extraction, embedding, retries)
        assert run.status == "retry_scheduled"
        assert run.error_detail["retry_count"] == retries + 1

    with pytest.raises(ProviderError):
        await run_attempt(db_session, tmp_path, run, extraction, embedding, 3)

    assert extraction.call_count == 4
    assert run.status == "failed"
    assert run.error_code == "provider_unavailable"
    assert run.error_detail["retry_count"] == 3
    assert run.error_detail["max_retries"] == 3
    assert run.finished_at is not None


async def test_run_completes_when_a_retry_succeeds(db_session, tmp_path):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [ProviderError("provider_unavailable", retryable=True)]

    with pytest.raises(ProviderError):
        await run_attempt(db_session, tmp_path, run, extraction, embedding, 0)
    assert run.status == "retry_scheduled"

    await run_attempt(db_session, tmp_path, run, extraction, embedding, 1)

    assert run.status == "awaiting_review"
    assert run.error_code is None
    assert run.error_detail == {}


async def test_non_retryable_error_fails_immediately(db_session, tmp_path):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [ProviderError("provider_auth_failed", retryable=False)]

    with pytest.raises(ProviderError):
        await run_attempt(db_session, tmp_path, run, extraction, embedding, 0)

    assert run.status == "failed"
    assert run.error_code == "provider_auth_failed"
    assert "retry_count" not in run.error_detail
    assert run.finished_at is not None
