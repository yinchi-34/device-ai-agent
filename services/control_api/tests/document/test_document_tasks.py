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


def _fake_execute(monkeypatch, fail_times):
    from app.modules.document import tasks

    seen = []

    async def fake(run_id, retry_count=0):
        seen.append(retry_count)
        if len(seen) <= fail_times:
            raise tasks.TransientProviderError("provider_timeout")

    monkeypatch.setattr(tasks, "_execute", fake)
    return seen


def test_task_passes_celery_retry_count_and_stops_after_three_retries(monkeypatch):
    seen = _fake_execute(monkeypatch, fail_times=99)

    result = extract_document.apply(args=[1])

    assert seen == [0, 1, 2, 3]
    assert result.failed()


def test_task_succeeds_when_a_retry_works(monkeypatch):
    seen = _fake_execute(monkeypatch, fail_times=2)

    result = extract_document.apply(args=[1])

    assert seen == [0, 1, 2]
    assert result.successful()
