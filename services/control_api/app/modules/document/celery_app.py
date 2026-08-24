from celery import Celery

from app.core.config import Settings

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
)
