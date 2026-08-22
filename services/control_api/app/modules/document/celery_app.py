from celery import Celery

from app.core.config import Settings

settings = Settings()

celery_app = Celery("device_document", broker=settings.redis_url)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_ignore_result=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
)
