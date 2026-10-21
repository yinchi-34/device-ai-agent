import pytest

from app.modules.document import tasks
from app.modules.document.extraction import execute_extraction_run
from app.modules.document.providers import (
    FakeVisionOcrProvider,
    ProviderError,
)
from app.modules.document.storage import FileSystemDocumentStorage
from tests.document.test_extraction_pipeline import (
    pipeline_setup,
)


async def execute(db_session, tmp_path, run, extraction, embedding, **kwargs):
    return await execute_extraction_run(
        db_session,
        run.id,
        storage=FileSystemDocumentStorage(tmp_path),
        extraction_provider=extraction,
        vision_provider=FakeVisionOcrProvider([]),
        embedding_provider=embedding,
        max_retries=3,
        **kwargs,
    )


@pytest.mark.parametrize("retries_done", [0, 1, 2])
async def test_retryable_error_schedules_retry(db_session, tmp_path, retries_done):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [ProviderError("provider_rate_limited", retryable=True)]

    with pytest.raises(ProviderError):
        await execute(
            db_session, tmp_path, run, extraction, embedding, retries_done=retries_done
        )

    await db_session.refresh(run)
    assert run.status == "retry_scheduled"
    assert run.error_code == "provider_rate_limited"
    assert run.error_detail["retry_count"] == retries_done + 1
    assert run.error_detail["max_retries"] == 3
    assert run.finished_at is None


async def test_retries_exhausted_marks_failed(db_session, tmp_path):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [ProviderError("provider_rate_limited", retryable=True)]

    with pytest.raises(ProviderError):
        await execute(db_session, tmp_path, run, extraction, embedding, retries_done=3)

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
        await execute(db_session, tmp_path, run, extraction, embedding)

    await db_session.refresh(run)
    assert run.status == "failed"
    assert run.error_code == "provider_auth_failed"
    assert "retry_count" not in run.error_detail


async def test_success_after_retry_completes_and_clears_error(db_session, tmp_path):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [ProviderError("provider_rate_limited", retryable=True)]
    with pytest.raises(ProviderError):
        await execute(db_session, tmp_path, run, extraction, embedding)

    await execute(db_session, tmp_path, run, extraction, embedding, retries_done=1)

    await db_session.refresh(run)
    assert run.status == "awaiting_review"
    assert run.error_code is None
    assert run.error_detail == {}


def test_task_passes_celery_retry_count_and_stops_after_three(monkeypatch):
    seen = []

    async def fake_execute(run_id, retries_done=0):
        seen.append(retries_done)
        raise tasks.TransientProviderError("provider_timeout")

    monkeypatch.setattr(tasks, "_execute", fake_execute)
    monkeypatch.setattr(tasks.extract_document, "retry_backoff", False)
    monkeypatch.setattr(tasks.extract_document, "retry_jitter", False)

    result = tasks.extract_document.apply(args=(1,))

    assert seen == [0, 1, 2, 3]
    assert result.failed()
