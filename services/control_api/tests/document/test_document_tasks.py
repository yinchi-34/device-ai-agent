import subprocess
import sys

from app.modules.document.celery_app import celery_app
from app.modules.document.tasks import extract_document


def test_celery_uses_durable_json_tasks():
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.task_serializer == "json"
    assert celery_app.conf.accept_content == ["json"]
    assert celery_app.conf.broker_transport_options["visibility_timeout"] == 21600


def test_extract_task_accepts_only_run_identifier():
    assert extract_document.name == "document.extract"
    assert list(extract_document.run.__annotations__) == ["run_id", "return"]


def test_worker_default_modules_register_extraction_task():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from app.modules.document.celery_app import celery_app; "
                "celery_app.loader.import_default_modules(); "
                "print('document.extract' in celery_app.tasks)"
            ),
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "True"


def test_extract_task_retries_up_to_three_times_and_passes_retry_count(monkeypatch):
    from app.modules.document import tasks

    seen = []

    async def fake_execute(run_id, retry_count=0):
        seen.append(retry_count)
        raise tasks.TransientProviderError("provider_rate_limited")

    monkeypatch.setattr(tasks, "_execute", fake_execute)
    assert extract_document.max_retries == 3

    result = extract_document.apply(args=[1])

    assert seen == [0, 1, 2, 3]
    assert isinstance(result.result, tasks.TransientProviderError)


def test_extract_task_stops_retrying_after_success(monkeypatch):
    from app.modules.document import tasks

    seen = []

    async def fake_execute(run_id, retry_count=0):
        seen.append(retry_count)
        if retry_count < 2:
            raise tasks.TransientProviderError("provider_rate_limited")

    monkeypatch.setattr(tasks, "_execute", fake_execute)

    result = extract_document.apply(args=[1])

    assert seen == [0, 1, 2]
    assert result.successful()
