import pytest

from app.modules.document import tasks
from app.modules.document.extraction import execute_extraction_run
from app.modules.document.providers import FakeVisionOcrProvider, ProviderError
from app.modules.document.storage import FileSystemDocumentStorage
from tests.document.test_extraction_pipeline import pipeline_setup


async def _execute(db_session, tmp_path, run, extraction, embedding, task_retries):
    return await execute_extraction_run(
        db_session,
        run.id,
        storage=FileSystemDocumentStorage(tmp_path),
        extraction_provider=extraction,
        vision_provider=FakeVisionOcrProvider([]),
        embedding_provider=embedding,
        task_retries=task_retries,
        max_retries=3,
    )


@pytest.mark.parametrize("task_retries", [0, 1, 2])
async def test_retryable_error_schedules_retry(db_session, tmp_path, task_retries):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [ProviderError("provider_server_error", retryable=True)]

    with pytest.raises(ProviderError):
        await _execute(db_session, tmp_path, run, extraction, embedding, task_retries)

    await db_session.refresh(run)
    assert run.status == "retry_scheduled"
    assert run.error_code == "provider_server_error"
    assert run.error_detail["retry_count"] == task_retries + 1
    assert run.error_detail["max_retries"] == 3
    assert run.finished_at is None


async def test_retries_exhausted_fails(db_session, tmp_path):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [ProviderError("provider_rate_limited", retryable=True)]

    with pytest.raises(ProviderError):
        await _execute(db_session, tmp_path, run, extraction, embedding, 3)

    await db_session.refresh(run)
    assert run.status == "failed"
    assert run.error_code == "provider_rate_limited"
    assert run.error_detail["retry_count"] == 3
    assert run.error_detail["max_retries"] == 3
    assert run.finished_at is not None


async def test_non_retryable_error_fails_immediately(db_session, tmp_path):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [ProviderError("provider_bad_request", retryable=False)]

    with pytest.raises(ProviderError):
        await _execute(db_session, tmp_path, run, extraction, embedding, 0)

    await db_session.refresh(run)
    assert run.status == "failed"
    assert "retry_count" not in run.error_detail


async def test_success_after_retry_completes_and_clears_error(db_session, tmp_path):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [ProviderError("provider_server_error", retryable=True)]
    with pytest.raises(ProviderError):
        await _execute(db_session, tmp_path, run, extraction, embedding, 0)

    await _execute(db_session, tmp_path, run, extraction, embedding, 1)

    await db_session.refresh(run)
    assert run.status == "awaiting_review"
    assert run.error_code is None
    assert run.error_detail == {}


def test_task_passes_celery_retry_count(monkeypatch):
    seen = []

    async def fake_execute(run_id, task_retries):
        seen.append((run_id, task_retries))

    monkeypatch.setattr(tasks, "_execute", fake_execute)
    tasks.extract_document.apply(args=(7,), retries=2).get()

    assert seen == [(7, 2)]
    assert tasks.extract_document.max_retries == 3
