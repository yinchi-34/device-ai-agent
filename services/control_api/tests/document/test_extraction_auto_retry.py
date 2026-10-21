import pytest

from app.modules.document import tasks
from app.modules.document.extraction import execute_extraction_run
from app.modules.document.providers import (
    FakeVisionOcrProvider,
    ProviderError,
)
from app.modules.document.storage import FileSystemDocumentStorage
from tests.document.test_extraction_pipeline import pipeline_setup


async def execute(db_session, tmp_path, run, extraction, embedding, retries_done):
    return await execute_extraction_run(
        db_session,
        run.id,
        storage=FileSystemDocumentStorage(tmp_path),
        extraction_provider=extraction,
        vision_provider=FakeVisionOcrProvider([]),
        embedding_provider=embedding,
        auto_retries_done=retries_done,
        max_auto_retries=3,
    )


@pytest.mark.parametrize("retries_done", [0, 1, 2])
async def test_retryable_error_schedules_retry(db_session, tmp_path, retries_done):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [ProviderError("provider_rate_limited", retryable=True)]

    with pytest.raises(ProviderError):
        await execute(db_session, tmp_path, run, extraction, embedding, retries_done)

    await db_session.refresh(run)
    assert run.status == "retry_scheduled"
    assert run.error_code == "provider_rate_limited"
    assert run.error_detail["retry_count"] == retries_done + 1
    assert run.error_detail["max_retries"] == 3
    assert run.finished_at is None


async def test_retries_exhausted_marks_failed(db_session, tmp_path):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [ProviderError("provider_unavailable", retryable=True)]

    with pytest.raises(ProviderError):
        await execute(db_session, tmp_path, run, extraction, embedding, 3)

    await db_session.refresh(run)
    assert run.status == "failed"
    assert run.error_code == "provider_unavailable"
    assert run.error_detail["retry_count"] == 3
    assert run.error_detail["max_retries"] == 3
    assert run.finished_at is not None


async def test_non_retryable_error_fails_immediately(db_session, tmp_path):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [ProviderError("provider_auth_failed", retryable=False)]

    with pytest.raises(ProviderError):
        await execute(db_session, tmp_path, run, extraction, embedding, 0)

    await db_session.refresh(run)
    assert run.status == "failed"
    assert run.error_code == "provider_auth_failed"
    assert "retry_count" not in run.error_detail


async def test_success_after_scheduled_retry_completes(db_session, tmp_path):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [ProviderError("provider_unavailable", retryable=True)]
    with pytest.raises(ProviderError):
        await execute(db_session, tmp_path, run, extraction, embedding, 0)
    await db_session.refresh(run)
    assert run.status == "retry_scheduled"

    await execute(db_session, tmp_path, run, extraction, embedding, 1)

    await db_session.refresh(run)
    assert run.status == "awaiting_review"
    assert run.error_code is None
    assert run.error_detail == {}


@pytest.fixture
def raising_executor(monkeypatch):
    seen = []

    async def fake_execute(session, run_id, **kwargs):
        seen.append(kwargs)
        raise ProviderError("provider_timeout", retryable=True)

    monkeypatch.setattr(tasks, "execute_extraction_run", fake_execute)
    return seen


@pytest.mark.parametrize("retries", [0, 1, 2])
async def test_task_asks_celery_to_retry_while_budget_remains(
    raising_executor, retries
):
    with pytest.raises(tasks.TransientProviderError):
        await tasks._execute(1, retries)

    assert raising_executor[0]["auto_retries_done"] == retries
    assert raising_executor[0]["max_auto_retries"] == 3


async def test_task_stops_retrying_after_budget(raising_executor):
    with pytest.raises(ProviderError):
        await tasks._execute(1, 3)


def test_task_retry_budget_matches_celery_config():
    assert tasks.extract_document.max_retries == tasks.MAX_AUTO_RETRIES == 3
