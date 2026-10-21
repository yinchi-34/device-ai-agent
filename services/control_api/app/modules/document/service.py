from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.modules.document.enums import ExtractionRunStatus
from app.modules.document.models import ProfileExtractionRun, SourceDocument
from app.modules.document.review_service import (
    DocumentReviewService as DocumentService,
)

__all__ = [
    "DocumentService",
    "create_extraction_run",
    "retry_extraction_run",
]


async def create_extraction_run(
    session: AsyncSession,
    source_document_id: int,
    *,
    extraction_provider: str,
    extraction_model: str,
    embedding_provider: str,
    embedding_model: str,
    vision_provider: str | None = None,
    vision_model: str | None = None,
    retry_of_run_id: int | None = None,
    attempt_number: int = 1,
) -> ProfileExtractionRun:
    if await session.get(SourceDocument, source_document_id) is None:
        raise DomainError(
            "not_found", f"SourceDocument {source_document_id} not found", 404
        )
    run = ProfileExtractionRun(
        source_document_id=source_document_id,
        retry_of_run_id=retry_of_run_id,
        status=ExtractionRunStatus.QUEUED.value,
        attempt_number=attempt_number,
        parser_version="1",
        prompt_version="5",
        candidate_schema_version="1.0",
        extraction_provider=extraction_provider,
        extraction_model=extraction_model,
        vision_provider=vision_provider,
        vision_model=vision_model,
        embedding_provider=embedding_provider,
        embedding_model=embedding_model,
    )
    session.add(run)
    await session.flush()
    return run


async def retry_extraction_run(
    session: AsyncSession, failed_run_id: int
) -> ProfileExtractionRun:
    failed_run = await session.get(ProfileExtractionRun, failed_run_id)
    if failed_run is None:
        raise DomainError("not_found", f"Extraction run {failed_run_id} not found", 404)
    # retry_scheduled is not failed: Celery is still retrying that run.
    if failed_run.status != ExtractionRunStatus.FAILED.value:
        raise DomainError(
            "extraction_run_not_failed", "Only a failed run can be retried", 409
        )
    return await create_extraction_run(
        session,
        failed_run.source_document_id,
        extraction_provider=failed_run.extraction_provider,
        extraction_model=failed_run.extraction_model,
        embedding_provider=failed_run.embedding_provider,
        embedding_model=failed_run.embedding_model,
        vision_provider=failed_run.vision_provider,
        vision_model=failed_run.vision_model,
        retry_of_run_id=failed_run.id,
        attempt_number=failed_run.attempt_number + 1,
    )
