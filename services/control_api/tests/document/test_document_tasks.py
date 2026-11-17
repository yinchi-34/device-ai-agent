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


def test_extract_task_logs_start_and_finish(monkeypatch, caplog):
    import logging

    from app.modules.document import tasks

    async def fake_execute(run_id):
        return None

    monkeypatch.setattr(tasks, "_execute", fake_execute)
    caplog.set_level(logging.INFO, logger="app.tasks")

    extract_document.apply(args=[42], task_id="task-1")

    events = [(r.getMessage(), r.run_id, r.task_id) for r in caplog.records]
    assert ("extraction_task_started", 42, "task-1") in events
    assert ("extraction_task_finished", 42, "task-1") in events


def test_extract_task_logs_transient_errors_before_retrying(monkeypatch, caplog):
    import logging

    from app.modules.document import tasks
    from app.modules.document.providers import ProviderError

    async def rate_limited(run_id):
        raise ProviderError("provider_rate_limited", retryable=True, status_code=429)

    monkeypatch.setattr(tasks, "_execute", rate_limited)
    caplog.set_level(logging.INFO, logger="app.tasks")

    extract_document.apply(args=[42], task_id="task-2")

    retrying = [r for r in caplog.records if r.getMessage() == "extraction_task_retrying"]
    assert retrying
    assert retrying[0].error_code == "provider_rate_limited"
    assert retrying[0].run_id == 42


def test_worker_logging_uses_json():
    from celery.signals import setup_logging

    from app.modules.document import celery_app as celery_app_module

    # Connecting to setup_logging stops celery from installing its own handlers.
    assert setup_logging.has_listeners()
    assert callable(celery_app_module._configure_worker_logging)
