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


def test_task_passes_celery_retry_number_and_stops_after_max_retries(monkeypatch):
    from app.modules.document import tasks
    from app.modules.document.providers import ProviderError

    seen = []

    async def always_unavailable(session, run_id, **kwargs):
        seen.append(kwargs["retries"])
        raise ProviderError("provider_unavailable", retryable=True)

    monkeypatch.setattr(tasks, "execute_extraction_run", always_unavailable)
    monkeypatch.setattr(extract_document, "retry_backoff", False)

    result = extract_document.apply(args=(1,))

    assert seen == [0, 1, 2, 3]
    assert isinstance(result.result, ProviderError)
