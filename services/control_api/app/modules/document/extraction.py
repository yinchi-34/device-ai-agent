from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.modules.document.candidate_validation import (
    validate_candidate_domain,
    validate_candidate_schema,
)
from app.modules.document.chunking import build_chunks
from app.modules.document.enums import CandidateStatus, ExtractionRunStatus
from app.modules.document.models import (
    DocumentChunk,
    ProfileExtractionCandidate,
    ProfileExtractionRun,
)
from app.modules.document.parsers import (
    Pypdfium2PageRenderer,
    parse_docx,
    parse_pdf,
)
from app.modules.document.providers import (
    EmbeddingProvider,
    ExtractionModelProvider,
    PdfPageRenderer,
    ProviderError,
    VisionOcrProvider,
)
from app.modules.document.retrieval import embed_run_chunks
from app.modules.document.storage import DocumentStorage


async def _transition(
    session: AsyncSession, run: ProfileExtractionRun, status: ExtractionRunStatus
) -> None:
    run.status = status.value
    if status == ExtractionRunStatus.VALIDATING and run.started_at is None:
        run.started_at = datetime.now(UTC)
    await session.commit()


def _hash_storage_file(storage: DocumentStorage, storage_uri: str) -> str:
    digest = sha256()
    with storage.open(storage_uri) as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _storage_path(storage: DocumentStorage, storage_uri: str) -> Path:
    with storage.open(storage_uri) as stream:
        name = getattr(stream, "name", None)
    if not name:
        raise RuntimeError("Document storage stream must expose a local path")
    return Path(name)


async def execute_extraction_run(
    session: AsyncSession,
    run_id: int,
    *,
    storage: DocumentStorage,
    extraction_provider: ExtractionModelProvider,
    vision_provider: VisionOcrProvider,
    embedding_provider: EmbeddingProvider,
    renderer: PdfPageRenderer | None = None,
    min_text_chars: int = 40,
) -> ProfileExtractionRun:
    statement = (
        select(ProfileExtractionRun)
        .where(ProfileExtractionRun.id == run_id)
        .options(
            selectinload(ProfileExtractionRun.source_document),
            selectinload(ProfileExtractionRun.candidate),
        )
        .with_for_update()
    )
    run = await session.scalar(statement)
    if run is None:
        raise ValueError(f"Extraction run {run_id} not found")
    if run.status in {
        ExtractionRunStatus.AWAITING_REVIEW.value,
        ExtractionRunStatus.COMPLETED.value,
        ExtractionRunStatus.CANCELED.value,
    }:
        return run
    try:
        await _transition(session, run, ExtractionRunStatus.VALIDATING)
        document = run.source_document
        if _hash_storage_file(storage, document.storage_uri) != document.file_hash_sha256:
            raise ValueError("Source document hash mismatch")

        await _transition(session, run, ExtractionRunStatus.PARSING)
        path = _storage_path(storage, document.storage_uri)
        if document.media_type == "application/pdf":
            parsed = await parse_pdf(
                path,
                renderer=renderer or Pypdfium2PageRenderer(),
                vision=vision_provider,
                min_text_chars=min_text_chars,
            )
        else:
            parsed = parse_docx(path)
        normalized_chunks = build_chunks(parsed)
        await session.execute(
            delete(DocumentChunk).where(DocumentChunk.extraction_run_id == run.id)
        )
        for chunk in normalized_chunks:
            session.add(
                DocumentChunk(
                    source_document_id=document.id,
                    extraction_run_id=run.id,
                    **chunk.__dict__,
                )
            )
        run.parse_metadata = parsed.metadata
        await session.commit()
        await embed_run_chunks(session, run.id, embedding_provider)
        await session.commit()

        await _transition(session, run, ExtractionRunStatus.EXTRACTING)
        extraction_result = await extraction_provider.extract(
            normalized_chunks,
            {
                "source_document_id": document.id,
                "media_type": document.media_type,
            },
        )
        run.raw_agent_response = extraction_result.raw_response
        run.usage_metadata = extraction_result.usage

        await _transition(session, run, ExtractionRunStatus.VALIDATING_OUTPUT)
        schema_errors = validate_candidate_schema(extraction_result.payload)
        domain_errors = []
        if not schema_errors:
            domain_errors = await validate_candidate_domain(
                session,
                extraction_result.payload,
                {chunk.stable_ref for chunk in normalized_chunks},
            )
        status = (
            CandidateStatus.VALID
            if not schema_errors and not domain_errors
            else CandidateStatus.INVALID
        )
        candidate = run.candidate
        if candidate is None:
            candidate = ProfileExtractionCandidate(
                extraction_run_id=run.id,
                original_payload=extraction_result.payload,
                reviewed_payload=extraction_result.payload.copy(),
            )
            session.add(candidate)
            run.candidate = candidate
        candidate.schema_errors = schema_errors
        candidate.domain_errors = domain_errors
        candidate.status = status.value
        run.status = ExtractionRunStatus.AWAITING_REVIEW.value
        run.error_code = None
        run.error_detail = {}
        await session.commit()
        return run
    except ProviderError as exc:
        run.status = ExtractionRunStatus.FAILED.value
        run.error_code = exc.code
        run.error_detail = {"retryable": exc.retryable}
        run.finished_at = datetime.now(UTC)
        await session.commit()
        raise
    except Exception as exc:
        run.status = ExtractionRunStatus.FAILED.value
        run.error_code = "extraction_failed"
        run.error_detail = {"error_type": type(exc).__name__}
        run.finished_at = datetime.now(UTC)
        await session.commit()
        raise


async def execute_extraction_with_retries(
    session: AsyncSession,
    run_id: int,
    *,
    max_attempts: int,
    **dependencies,
) -> ProfileExtractionRun:
    last_error: ProviderError | None = None
    for _ in range(max_attempts):
        try:
            return await execute_extraction_run(
                session, run_id, **dependencies
            )
        except ProviderError as exc:
            if not exc.retryable:
                raise
            last_error = exc
    if last_error is None:
        raise RuntimeError("Extraction retry loop did not execute")
    raise last_error
