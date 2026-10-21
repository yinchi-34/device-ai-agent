import asyncio

from app.core.config import Settings
from app.db.session import async_session_factory
from app.modules.document.celery_app import celery_app
from app.modules.document.extraction import execute_extraction_run
from app.modules.document.openai_compatible import (
    OpenAICompatibleEmbeddingProvider,
    OpenAICompatibleExtractionProvider,
    OpenAICompatibleVisionProvider,
)
from app.modules.document.providers import ProviderError
from app.modules.document.storage import FileSystemDocumentStorage

MAX_RETRIES = 3


class TransientProviderError(RuntimeError):
    pass


async def _execute(run_id: int, retry_count: int = 0) -> None:
    settings = Settings()
    async with async_session_factory() as session:
        try:
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
                retry_count=retry_count,
                max_retries=MAX_RETRIES,
            )
        except ProviderError as exc:
            if exc.retryable:
                raise TransientProviderError(exc.code) from None
            raise


@celery_app.task(
    name="document.extract",
    autoretry_for=(TransientProviderError,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=MAX_RETRIES,
    bind=True,
)
def extract_document(self, run_id: int) -> None:
    asyncio.run(_execute(run_id, self.request.retries))
