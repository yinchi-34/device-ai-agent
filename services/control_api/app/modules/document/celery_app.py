from celery import Celery
from celery.signals import setup_logging

from app.core.config import Settings
from app.core.logging import configure_logging

settings = Settings()

celery_app = Celery(
    "device_document",
    broker=settings.redis_url,
    include=["app.modules.document.tasks"],
)
celery_app.conf.update(
    broker_transport_options={
        "visibility_timeout": settings.document_task_visibility_timeout_seconds,
    },
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_ignore_result=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # Needs a `celery beat` process; see document-beat in docker-compose.yml.
    beat_schedule={
        "reap-stale-extraction-runs": {
            "task": "document.reap_stale_runs",
            "schedule": 600,
        },
    },
)


@setup_logging.connect
def _configure_worker_logging(**_kwargs: object) -> None:
    """Make the worker and beat log JSON like the API.

    Connecting to this signal also stops celery from installing its own handlers.
    """
    configure_logging()
