import pytest

from app.modules.document import tasks
from app.modules.document.extraction import (
    execute_extraction_run,
    execute_extraction_with_retries,
)
from app.modules.document.providers import FakeVisionOcrProvider, ProviderError
from app.modules.document.storage import FileSystemDocumentStorage
from tests.document.test_extraction_pipeline import pipeline_setup


def throttled():
    return ProviderError("provider_rate_limited", retryable=True, status_code=429)


async def execute(db_session, tmp_path, run, extraction, embedding, **options):
    return await execute_extraction_run(
        db_session,
        run.id,
        storage=FileSystemDocumentStorage(tmp_path),
        extraction_provider=extraction,
        vision_provider=FakeVisionOcrProvider([]),
        embedding_provider=embedding,
        **options,
    )


@pytest.mark.parametrize("retry_count", [0, 1, 2])
async def test_retryable_error_schedules_retry(db_session, tmp_path, retry_count):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [throttled()]

    with pytest.raises(ProviderError):
        await execute(
            db_session,
            tmp_path,
            run,
            extraction,
            embedding,
            retry_count=retry_count,
            max_retries=3,
        )

    await db_session.refresh(run)
    assert run.status == "retry_scheduled"
    assert run.error_code == "provider_rate_limited"
    assert run.error_detail["retry_count"] == retry_count + 1
    assert run.error_detail["max_retries"] == 3
    assert run.finished_at is None


async def test_retryable_error_fails_when_retries_are_used_up(db_session, tmp_path):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [throttled()]

    with pytest.raises(ProviderError):
        await execute(
            db_session, tmp_path, run, extraction, embedding, retry_count=3, max_retries=3
        )

    await db_session.refresh(run)
    assert run.status == "failed"
    assert run.error_code == "provider_rate_limited"
    assert run.error_detail["retry_count"] == 3
    assert run.error_detail["max_retries"] == 3
    assert run.finished_at is not None


async def test_non_retryable_error_fails_immediately(db_session, tmp_path):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [ProviderError("provider_auth_failed", retryable=False)]

    with pytest.raises(ProviderError):
        await execute(
            db_session, tmp_path, run, extraction, embedding, retry_count=0, max_retries=3
        )

    await db_session.refresh(run)
    assert run.status == "failed"
    assert run.error_code == "provider_auth_failed"
    assert "retry_count" not in run.error_detail


async def test_success_after_scheduled_retry_completes_cleanly(db_session, tmp_path):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [throttled(), throttled()]

    await execute_extraction_with_retries(
        db_session,
        run.id,
        storage=FileSystemDocumentStorage(tmp_path),
        extraction_provider=extraction,
        vision_provider=FakeVisionOcrProvider([]),
        embedding_provider=embedding,
        max_attempts=4,
    )

    await db_session.refresh(run)
    assert run.status == "awaiting_review"
    assert run.error_code is None
    assert run.error_detail == {}


async def test_persistent_retryable_error_ends_failed_after_three_retries(
    db_session, tmp_path
):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [throttled() for _ in range(10)]

    with pytest.raises(ProviderError):
        await execute_extraction_with_retries(
            db_session,
            run.id,
            storage=FileSystemDocumentStorage(tmp_path),
            extraction_provider=extraction,
            vision_provider=FakeVisionOcrProvider([]),
            embedding_provider=embedding,
            max_attempts=4,
        )

    await db_session.refresh(run)
    assert extraction.call_count == 4
    assert run.status == "failed"
    assert run.error_detail["retry_count"] == 3


def test_celery_task_passes_retry_count_and_stops_after_three_retries(monkeypatch):
    seen = []

    async def fake_execute(session, run_id, **kwargs):
        seen.append((kwargs["retry_count"], kwargs["max_retries"]))
        raise ProviderError("provider_rate_limited", retryable=True)

    monkeypatch.setattr(tasks, "execute_extraction_run", fake_execute)

    result = tasks.extract_document.apply(args=[1])

    assert seen == [(0, 3), (1, 3), (2, 3), (3, 3)]
    assert isinstance(result.result, tasks.TransientProviderError)
