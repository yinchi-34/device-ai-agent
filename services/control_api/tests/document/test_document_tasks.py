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


def test_task_passes_celery_retry_count_to_pipeline(monkeypatch):
    from app.modules.document import tasks

    seen = {}

    async def fake_execute(session, run_id, **kwargs):
        seen.update(kwargs, run_id=run_id)

    monkeypatch.setattr(tasks, "execute_extraction_run", fake_execute)
    extract_document.push_request(retries=2)
    try:
        extract_document.run(7)
    finally:
        extract_document.pop_request()

    assert (seen["run_id"], seen["retries"], seen["max_retries"]) == (7, 2, 3)
