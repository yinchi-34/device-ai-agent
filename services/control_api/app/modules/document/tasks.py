import asyncio
import logging
import time

from app.core.config import Settings
from app.db.session import async_session_factory
from app.modules.document.celery_app import celery_app
from app.modules.document.extraction import (
    execute_extraction_run,
    reap_stale_extraction_runs,
)
from app.modules.document.openai_compatible import (
    OpenAICompatibleEmbeddingProvider,
    OpenAICompatibleExtractionProvider,
    OpenAICompatibleVisionProvider,
)
from app.modules.document.providers import ProviderError
from app.modules.document.storage import FileSystemDocumentStorage

task_logger = logging.getLogger("app.tasks")


class TransientProviderError(RuntimeError):
    pass


async def _execute(run_id: int) -> None:
    settings = Settings()
    async with async_session_factory() as session:
        await execute_extraction_run(
            session,
            run_id,
            storage=FileSystemDocumentStorage(settings.document_storage_root),
            extraction_provider=OpenAICompatibleExtractionProvider(
                base_url=settings.document_llm_base_url,
                api_key=settings.document_llm_api_key,
                model=settings.document_llm_model,
                timeout_seconds=settings.document_llm_timeout_seconds,
                max_output_tokens=settings.document_llm_max_output_tokens,
            ),
            vision_provider=OpenAICompatibleVisionProvider(
                base_url=settings.document_vision_base_url,
                api_key=settings.document_vision_api_key,
                model=settings.document_vision_model,
                timeout_seconds=settings.document_vision_timeout_seconds,
            ),
            embedding_provider=OpenAICompatibleEmbeddingProvider(
                base_url=settings.document_embedding_base_url,
                api_key=settings.document_embedding_api_key,
                model=settings.document_embedding_model,
                timeout_seconds=settings.document_embedding_timeout_seconds,
            ),
            min_text_chars=settings.document_min_text_chars_per_page,
            extraction_group_max_tokens=(
                settings.document_extraction_group_max_tokens
            ),
        )


@celery_app.task(
    name="document.extract",
    autoretry_for=(TransientProviderError,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
)
def extract_document(run_id: int) -> None:
    request = extract_document.request
    fields = {"run_id": run_id, "task_id": request.id, "retries": request.retries}
    task_logger.info("extraction_task_started", extra=fields)
    started = time.perf_counter()
    try:
        asyncio.run(_execute(run_id))
    except ProviderError as exc:
        if exc.retryable:
            # Celery retries TransientProviderError with backoff, up to max_retries.
            task_logger.warning(
                "extraction_task_retrying", extra={**fields, "error_code": exc.code}
            )
            raise TransientProviderError(exc.code) from None
        task_logger.warning(
            "extraction_task_failed", extra={**fields, "error_code": exc.code}
        )
        raise
    except Exception as exc:
        task_logger.exception(
            "extraction_task_failed",
            extra={**fields, "error_type": type(exc).__name__},
        )
        raise
    task_logger.info(
        "extraction_task_finished",
        extra={**fields, "duration_ms": round((time.perf_counter() - started) * 1000, 1)},
    )


async def _reap() -> list[int]:
    settings = Settings()
    async with async_session_factory() as session:
        return await reap_stale_extraction_runs(
            session,
            stale_queued_seconds=settings.document_stale_queued_seconds,
            stale_running_seconds=settings.document_stale_running_seconds,
        )


@celery_app.task(name="document.reap_stale_runs")
def reap_stale_runs() -> list[int]:
    """Run by celery beat; see reap_stale_extraction_runs."""
    return asyncio.run(_reap())
