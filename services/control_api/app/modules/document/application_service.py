from collections.abc import AsyncIterable, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import DomainError
from app.modules.device.models import DeviceModel
from app.modules.document.enums import ExtractionRunStatus
from app.modules.document.file_validation import DocumentValidationError
from app.modules.document.models import (
    ProfileExtractionCandidate,
    ProfileExtractionRun,
    SourceDocument,
)
from app.modules.document.providers import EmbeddingProvider
from app.modules.document.retrieval import search_document_chunks
from app.modules.document.service import create_extraction_run, retry_extraction_run
from app.modules.document.storage import (
    FileSystemDocumentStorage,
    validate_and_promote,
)

FINISHED_RUN_STATUSES = (
    ExtractionRunStatus.COMPLETED.value,
    ExtractionRunStatus.FAILED.value,
    ExtractionRunStatus.CANCELED.value,
)


class DocumentApplicationService:
    def __init__(self, session: AsyncSession, settings: Settings | None = None):
        self.session = session
        self.settings = settings or Settings()
        self.storage = FileSystemDocumentStorage(self.settings.document_storage_root)

    async def upload(
        self,
        *,
        original_file_name: str,
        content_type: str | None,
        chunks: AsyncIterable[bytes] | Iterable[bytes],
        device_model_id: int | None,
    ) -> SourceDocument:
        if device_model_id is not None and await self.session.get(
            DeviceModel, device_model_id
        ) is None:
            raise DomainError(
                "not_found", f"DeviceModel {device_model_id} not found", 404
            )
        promoted, validated = await validate_and_promote(
            self.storage,
            original_file_name=original_file_name,
            declared_media_type=content_type,
            chunks=chunks,
            max_file_bytes=self.settings.document_max_file_bytes,
            max_pdf_pages=self.settings.document_max_pdf_pages,
        )
        document = SourceDocument(
            device_model_id=device_model_id,
            original_file_name=original_file_name,
            media_type=validated.media_type,
            file_size_bytes=validated.size_bytes,
            file_hash_sha256=validated.file_hash_sha256,
            storage_uri=promoted.storage_uri,
            file_metadata=validated.metadata,
            validation_status="validated",
        )
        self.session.add(document)
        await self.session.flush()
        return document

    async def get_document(self, document_id: int) -> SourceDocument:
        document = await self.session.get(SourceDocument, document_id)
        if document is None or document.is_deleted:
            raise DomainError(
                "document_not_found", f"Document {document_id} not found", 404
            )
        return document

    async def queue_extraction(
        self, document_id: int
    ) -> tuple[ProfileExtractionRun, bool]:
        """Return the document's unfinished run, or create one.

        The document row is locked until the caller commits, so concurrent
        submissions for the same document are serialized and only the first
        creates a run. The flag is True when a new run was created.
        """
        document = await self.session.scalar(
            select(SourceDocument)
            .where(SourceDocument.id == document_id)
            .with_for_update()
        )
        if document is None or document.is_deleted:
            raise DomainError(
                "document_not_found", f"Document {document_id} not found", 404
            )
        active_run = await self.session.scalar(
            select(ProfileExtractionRun)
            .where(
                ProfileExtractionRun.source_document_id == document_id,
                ProfileExtractionRun.status.not_in(FINISHED_RUN_STATUSES),
            )
            .order_by(ProfileExtractionRun.id.desc())
            .limit(1)
        )
        if active_run is not None:
            return active_run, False
        run = await create_extraction_run(
            self.session,
            document_id,
            extraction_provider="openai_compatible",
            extraction_model=self.settings.document_llm_model,
            vision_provider="openai_compatible",
            vision_model=self.settings.document_vision_model,
            embedding_provider="openai_compatible",
            embedding_model=self.settings.document_embedding_model,
        )
        return run, True

    async def get_run(self, run_id: int) -> ProfileExtractionRun:
        run = await self.session.get(ProfileExtractionRun, run_id)
        if run is None:
            raise DomainError(
                "extraction_run_not_found", f"Extraction run {run_id} not found", 404
            )
        return run

    async def retry_run(self, run_id: int) -> ProfileExtractionRun:
        try:
            return await retry_extraction_run(self.session, run_id)
        except DomainError as exc:
            if exc.code == "extraction_run_not_failed":
                raise DomainError(
                    "extraction_run_not_retryable", exc.message, exc.status_code
                ) from None
            raise

    async def get_run_candidate(self, run_id: int) -> ProfileExtractionCandidate:
        await self.get_run(run_id)
        candidate = await self.session.scalar(
            select(ProfileExtractionCandidate).where(
                ProfileExtractionCandidate.extraction_run_id == run_id
            )
        )
        if candidate is None:
            raise DomainError(
                "candidate_not_found", f"Run {run_id} has no candidate", 404
            )
        return candidate

    async def search(
        self,
        *,
        query: str,
        source_document_id: int | None,
        device_model_id: int | None,
        provider: EmbeddingProvider,
        limit: int,
    ):
        return await search_document_chunks(
            self.session,
            query=query,
            source_document_id=source_document_id,
            device_model_id=device_model_id,
            provider=provider,
            limit=limit,
        )


def map_validation_error(exc: DocumentValidationError) -> DomainError:
    if exc.code in {"document_too_large", "document_pdf_too_many_pages"}:
        status_code = 413
    elif exc.code == "document_type_not_allowed":
        status_code = 415
    else:
        status_code = 422
    return DomainError(exc.code, exc.message, status_code)
