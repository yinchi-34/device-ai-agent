from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Identity, require_identity
from app.core.config import Settings
from app.core.errors import DomainError
from app.db.session import get_db_session
from app.modules.document import schemas
from app.modules.document.application_service import (
    DocumentApplicationService,
    map_validation_error,
)
from app.modules.document.file_validation import DocumentValidationError
from app.modules.document.openai_compatible import (
    OpenAICompatibleEmbeddingProvider,
)
from app.modules.document.providers import ProviderError
from app.modules.document.service import DocumentService
from app.modules.document.tasks import extract_document

router = APIRouter(prefix="/api/v1", tags=["document"])


async def iter_upload_file(file: UploadFile) -> AsyncIterator[bytes]:
    while chunk := await file.read(1024 * 1024):
        yield chunk


def dispatch_extraction_task(run_id: int) -> None:
    extract_document.delay(run_id)


def build_embedding_provider() -> OpenAICompatibleEmbeddingProvider:
    settings = Settings()
    return OpenAICompatibleEmbeddingProvider(
        base_url=settings.document_embedding_base_url,
        api_key=settings.document_embedding_api_key,
        model=settings.document_embedding_model,
        timeout_seconds=settings.document_embedding_timeout_seconds,
    )


@router.post("/documents", status_code=201, response_model=schemas.SourceDocumentOut)
async def upload_document(
    file: UploadFile = File(...),
    device_model_id: int | None = Form(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> object:
    try:
        document = await DocumentApplicationService(session).upload(
            original_file_name=file.filename or "upload",
            content_type=file.content_type,
            chunks=iter_upload_file(file),
            device_model_id=device_model_id,
        )
    except DocumentValidationError as exc:
        raise map_validation_error(exc) from None
    await session.commit()
    return document


@router.get("/documents/{document_id}", response_model=schemas.SourceDocumentOut)
async def get_document(
    document_id: int, session: AsyncSession = Depends(get_db_session)
) -> object:
    return await DocumentApplicationService(session).get_document(document_id)


@router.post(
    "/documents/{document_id}/extraction-runs",
    status_code=202,
    response_model=schemas.ExtractionRunOut,
)
async def queue_extraction(
    document_id: int, response: Response, session: AsyncSession = Depends(get_db_session)
) -> object:
    run, created = await DocumentApplicationService(session).queue_extraction(
        document_id
    )
    await session.commit()
    if not created:
        response.status_code = 200
        return run
    dispatch_extraction_task(run.id)
    return run


@router.get("/extraction-runs/{run_id}", response_model=schemas.ExtractionRunOut)
async def get_extraction_run(
    run_id: int, session: AsyncSession = Depends(get_db_session)
) -> object:
    return await DocumentApplicationService(session).get_run(run_id)


@router.post(
    "/extraction-runs/{run_id}/retry",
    status_code=202,
    response_model=schemas.ExtractionRunOut,
)
async def retry_extraction(
    run_id: int, session: AsyncSession = Depends(get_db_session)
) -> object:
    run = await DocumentApplicationService(session).retry_run(run_id)
    await session.commit()
    dispatch_extraction_task(run.id)
    return run


@router.get(
    "/extraction-runs/{run_id}/candidate", response_model=schemas.CandidateOut
)
async def get_candidate(
    run_id: int, session: AsyncSession = Depends(get_db_session)
) -> object:
    return await DocumentApplicationService(session).get_run_candidate(run_id)


@router.patch(
    "/extraction-candidates/{candidate_id}", response_model=schemas.CandidateOut
)
async def update_candidate(
    candidate_id: int,
    payload: schemas.CandidatePatch,
    session: AsyncSession = Depends(get_db_session),
    identity: Identity = Depends(require_identity),
) -> object:
    candidate = await DocumentService(session).update_candidate(
        candidate_id, revision=payload.revision, payload=payload.payload, actor=identity.actor
    )
    await session.commit()
    await session.refresh(candidate)
    return candidate


@router.post(
    "/extraction-candidates/{candidate_id}/validate",
    response_model=schemas.CandidateOut,
)
async def validate_candidate(
    candidate_id: int, session: AsyncSession = Depends(get_db_session)
) -> object:
    candidate = await DocumentService(session).validate_candidate(candidate_id)
    await session.commit()
    await session.refresh(candidate)
    return candidate


@router.post(
    "/extraction-candidates/{candidate_id}/approve",
    response_model=schemas.CandidateOut,
)
async def approve_candidate(
    candidate_id: int,
    payload: schemas.CandidateAction,
    session: AsyncSession = Depends(get_db_session),
    identity: Identity = Depends(require_identity),
) -> object:
    candidate = await DocumentService(session).approve_candidate(
        candidate_id, revision=payload.revision, actor=identity.actor
    )
    await session.commit()
    await session.refresh(candidate)
    return candidate


@router.post(
    "/extraction-candidates/{candidate_id}/reject",
    response_model=schemas.CandidateOut,
)
async def reject_candidate(
    candidate_id: int,
    payload: schemas.CandidateAction,
    session: AsyncSession = Depends(get_db_session),
    identity: Identity = Depends(require_identity),
) -> object:
    candidate = await DocumentService(session).reject_candidate(
        candidate_id,
        revision=payload.revision,
        note=payload.note or "",
        actor=identity.actor,
    )
    await session.commit()
    await session.refresh(candidate)
    return candidate


@router.post(
    "/extraction-candidates/{candidate_id}/import",
    status_code=201,
    response_model=schemas.CandidateImportOut,
)
async def import_candidate(
    candidate_id: int,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
    identity: Identity = Depends(require_identity),
) -> object:
    candidate = await DocumentService(session)._candidate_for_update(candidate_id)
    already_imported = candidate.imported_profile_version_id is not None
    profile = await DocumentService(session).import_candidate(candidate_id, actor=identity.actor)
    await session.commit()
    response.status_code = 200 if already_imported else 201
    return {"profile_version_id": profile.id}


@router.post(
    "/document-search", response_model=list[schemas.DocumentSearchResultOut]
)
async def document_search(
    payload: schemas.DocumentSearchRequest,
    session: AsyncSession = Depends(get_db_session),
) -> object:
    try:
        return await DocumentApplicationService(session).search(
            query=payload.query,
            source_document_id=payload.source_document_id,
            device_model_id=payload.device_model_id,
            provider=build_embedding_provider(),
            limit=payload.limit,
        )
    except ProviderError as exc:
        raise DomainError(exc.code, str(exc), 503) from None
