import subprocess
import sys

from app.modules.document import tasks
from app.modules.document.celery_app import celery_app
from app.modules.document.tasks import TransientProviderError, extract_document


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


def test_extract_task_retries_transient_errors_three_times():
    assert extract_document.max_retries == 3
    assert extract_document.autoretry_for == (TransientProviderError,)


def test_extract_task_passes_celery_retry_count_to_pipeline(monkeypatch):
    calls = []

    async def fake_execute(run_id, retry_count=0):
        calls.append((run_id, retry_count))

    monkeypatch.setattr(tasks, "_execute", fake_execute)
    extract_document.push_request(retries=2)
    try:
        extract_document.run(7)
    finally:
        extract_document.pop_request()

    assert calls == [(7, 2)]
