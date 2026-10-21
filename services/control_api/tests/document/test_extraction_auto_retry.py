import pytest

from app.modules.document import tasks
from app.modules.document.extraction import MAX_AUTO_RETRIES, execute_extraction_run
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


@pytest.mark.parametrize("retries_used", [0, 1, 2])
async def test_retryable_error_schedules_retry(db_session, tmp_path, retries_used):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [ProviderError("provider_rate_limited", retryable=True)]

    with pytest.raises(ProviderError):
        await _execute(
            db_session, tmp_path, run, extraction, embedding,
            auto_retries_used=retries_used,
        )

    await db_session.refresh(run)
    assert run.status == "retry_scheduled"
    assert run.error_code == "provider_rate_limited"
    assert run.error_detail["retry_count"] == retries_used + 1
    assert run.error_detail["max_retries"] == 3
    assert run.finished_at is None


async def test_exhausted_retries_fail_the_run(db_session, tmp_path):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [ProviderError("provider_rate_limited", retryable=True)]

    with pytest.raises(ProviderError):
        await _execute(
            db_session, tmp_path, run, extraction, embedding,
            auto_retries_used=MAX_AUTO_RETRIES,
        )

    await db_session.refresh(run)
    assert run.status == "failed"
    assert run.error_code == "provider_rate_limited"
    assert run.error_detail["retry_count"] == 3
    assert run.error_detail["max_retries"] == 3
    assert run.finished_at is not None


async def test_non_retryable_error_fails_immediately(db_session, tmp_path):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [ProviderError("provider_auth_error", retryable=False)]

    with pytest.raises(ProviderError):
        await _execute(
            db_session, tmp_path, run, extraction, embedding, auto_retries_used=0
        )

    await db_session.refresh(run)
    assert run.status == "failed"
    assert "retry_count" not in run.error_detail


async def test_unexpected_error_fails_immediately(db_session, tmp_path):
    run, _, embedding = await pipeline_setup(db_session, tmp_path)

    class Broken:
        async def extract(self, chunks, context):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await _execute(
            db_session, tmp_path, run, Broken(), embedding, auto_retries_used=0
        )

    await db_session.refresh(run)
    assert run.status == "failed"
    assert run.error_code == "extraction_failed"


async def test_without_auto_retry_context_retryable_error_fails(db_session, tmp_path):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [ProviderError("provider_rate_limited", retryable=True)]

    with pytest.raises(ProviderError):
        await _execute(db_session, tmp_path, run, extraction, embedding)

    await db_session.refresh(run)
    assert run.status == "failed"


async def test_success_after_scheduled_retry_completes_normally(db_session, tmp_path):
    run, extraction, embedding = await pipeline_setup(db_session, tmp_path)
    extraction.failures = [ProviderError("provider_rate_limited", retryable=True)]
    with pytest.raises(ProviderError):
        await _execute(
            db_session, tmp_path, run, extraction, embedding, auto_retries_used=0
        )
    await db_session.refresh(run)
    assert run.status == "retry_scheduled"

    await _execute(
        db_session, tmp_path, run, extraction, embedding, auto_retries_used=1
    )

    await db_session.refresh(run)
    assert run.status == "awaiting_review"
    assert run.error_code is None
    assert run.error_detail == {}


@pytest.fixture
def recorded_executions(monkeypatch):
    calls = []

    async def always_rate_limited(session, run_id, **kwargs):
        calls.append(kwargs["auto_retries_used"])
        raise ProviderError("provider_rate_limited", retryable=True)

    monkeypatch.setattr(tasks, "execute_extraction_run", always_rate_limited)
    return calls


async def test_task_passes_retry_count_and_stops_after_max_retries(
    recorded_executions,
):
    with pytest.raises(ProviderError):
        for retries in range(MAX_AUTO_RETRIES + 1):
            with pytest.raises(tasks.TransientProviderError):
                await tasks._execute(1, retries)

    assert recorded_executions == [0, 1, 2, 3]


def test_celery_retries_task_until_budget_is_spent(monkeypatch):
    calls = []

    async def fake_execute(run_id, retries):
        calls.append(retries)
        if retries < MAX_AUTO_RETRIES:
            raise tasks.TransientProviderError("provider_timeout")

    monkeypatch.setattr(tasks, "_execute", fake_execute)

    result = tasks.extract_document.apply(args=[1])

    assert result.successful()
    assert calls == [0, 1, 2, 3]
    assert tasks.extract_document.max_retries == MAX_AUTO_RETRIES
