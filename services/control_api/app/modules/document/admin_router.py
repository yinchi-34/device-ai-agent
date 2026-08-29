import hashlib
import hmac
import json
import secrets
from math import ceil
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import Settings
from app.core.errors import DomainError
from app.db.session import get_db_session
from app.modules.document.admin_review import (
    REVIEW_QUEUE_FILTERS,
    REVIEW_QUEUE_LABELS,
    ReviewQueueStatus,
    derive_review_queue_status,
    review_queue_priority,
)
from app.modules.document.application_service import (
    DocumentApplicationService,
    map_validation_error,
)
from app.modules.document.candidate_presenter import present_candidate
from app.modules.document.file_validation import DocumentValidationError
from app.modules.document.models import (
    CandidateReviewEvent,
    DocumentChunk,
    ProfileExtractionCandidate,
    ProfileExtractionRun,
    SourceDocument,
)
from app.modules.document.router import dispatch_extraction_task, iter_upload_file
from app.modules.document.service import DocumentService
from app.modules.document.storage import FileSystemDocumentStorage

security = HTTPBasic(auto_error=False)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
_CSRF_COOKIE = "admin_csrf"
_CSRF_HEADER = "x-csrf-token"


def require_admin(
    credentials: HTTPBasicCredentials | None = Depends(security),
) -> str:
    settings = Settings()
    if not settings.admin_password:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin review console is not configured",
        )
    valid = credentials is not None
    if valid:
        valid = secrets.compare_digest(
            credentials.username.encode(), settings.admin_username.encode()
        ) and secrets.compare_digest(
            credentials.password.encode(), settings.admin_password.encode()
        )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return settings.admin_username


def _sign_csrf_nonce(nonce: str, secret: str) -> str:
    return hmac.new(secret.encode(), nonce.encode(), hashlib.sha256).hexdigest()


def _new_csrf_token(secret: str) -> str:
    nonce = secrets.token_urlsafe(32)
    return f"{nonce}.{_sign_csrf_nonce(nonce, secret)}"


def _valid_csrf_token(token: str | None, secret: str) -> bool:
    if token is None or len(token) > 256:
        return False
    nonce, separator, signature = token.partition(".")
    if not separator or not nonce or not signature:
        return False
    expected = _sign_csrf_nonce(nonce, secret)
    return secrets.compare_digest(signature, expected)


async def require_admin_csrf(request: Request) -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    secret = Settings().admin_password
    cookie_token = request.cookies.get(_CSRF_COOKIE)
    submitted_token = request.headers.get(_CSRF_HEADER)
    if submitted_token is None:
        form = await request.form()
        raw_token = form.get("_csrf_token")
        submitted_token = raw_token if isinstance(raw_token, str) else None
    if (
        not _valid_csrf_token(cookie_token, secret)
        or submitted_token is None
        or not secrets.compare_digest(cookie_token, submitted_token)
    ):
        raise HTTPException(status_code=403, detail="Invalid admin CSRF token")


def _render_admin_template(
    request: Request, name: str, context: dict[str, Any]
) -> Response:
    secret = Settings().admin_password
    cookie_token = request.cookies.get(_CSRF_COOKIE)
    token = cookie_token if _valid_csrf_token(cookie_token, secret) else _new_csrf_token(secret)
    response = templates.TemplateResponse(
        request=request,
        name=name,
        context={**context, "csrf_token": token},
    )
    if token != cookie_token:
        response.set_cookie(
            _CSRF_COOKIE,
            token,
            max_age=8 * 60 * 60,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
            path="/admin",
        )
    return response


router = APIRouter(
    prefix="/admin",
    tags=["admin-review"],
    dependencies=[Depends(require_admin), Depends(require_admin_csrf)],
    include_in_schema=False,
)


def _candidate_url(candidate_id: int, message: str | None = None) -> str:
    url = f"/admin/candidates/{candidate_id}"
    return f"{url}?message={message}" if message else url


def _evidence_refs(value: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, dict):
        raw_refs = value.get("evidence_refs")
        if isinstance(raw_refs, list):
            refs.update(ref for ref in raw_refs if isinstance(ref, str))
        for child in value.values():
            refs.update(_evidence_refs(child))
    elif isinstance(value, list):
        for child in value:
            refs.update(_evidence_refs(child))
    return refs


async def _get_candidate(
    session: AsyncSession, candidate_id: int
) -> ProfileExtractionCandidate:
    candidate = await session.scalar(
        select(ProfileExtractionCandidate)
        .where(ProfileExtractionCandidate.id == candidate_id)
        .options(
            selectinload(ProfileExtractionCandidate.extraction_run).selectinload(
                ProfileExtractionRun.source_document
            )
        )
    )
    if candidate is None:
        raise DomainError("not_found", f"Candidate {candidate_id} not found", 404)
    return candidate


@router.get("")
async def admin_home() -> RedirectResponse:
    return RedirectResponse("/admin/documents", status_code=307)


@router.get("/documents")
async def document_list(
    request: Request,
    queue_status: ReviewQueueStatus | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    session: AsyncSession = Depends(get_db_session),
):
    documents = (
        await session.scalars(
            select(SourceDocument)
            .where(SourceDocument.is_deleted.is_(False))
            .options(
                selectinload(SourceDocument.extraction_runs).selectinload(
                    ProfileExtractionRun.candidate
                )
            )
            .order_by(SourceDocument.created_at.desc(), SourceDocument.id.desc())
        )
    ).unique().all()
    rows = []
    for document in documents:
        latest_run = max(document.extraction_runs, key=lambda run: run.id, default=None)
        candidate = latest_run.candidate if latest_run else None
        aggregate_status = derive_review_queue_status(
            document.validation_status,
            latest_run.status if latest_run else None,
            candidate.status if candidate else None,
        )
        rows.append(
            {
                "document": document,
                "run": latest_run,
                "candidate": candidate,
                "queue_status": aggregate_status,
                "queue_status_label": REVIEW_QUEUE_LABELS[aggregate_status],
                "error_message": (
                    latest_run.error_detail.get("message")
                    if latest_run and isinstance(latest_run.error_detail, dict)
                    else None
                ),
            }
        )
    rows.sort(key=lambda row: review_queue_priority(row["queue_status"]))
    if queue_status is not None:
        rows = [row for row in rows if row["queue_status"] == queue_status]
    page_size = 20
    total_rows = len(rows)
    total_pages = max(1, ceil(total_rows / page_size))
    page = min(page, total_pages)
    page_rows = rows[(page - 1) * page_size : page * page_size]
    return _render_admin_template(
        request,
        "admin/documents.html",
        {
            "rows": page_rows,
            "message": request.query_params.get("message"),
            "queue_filters": REVIEW_QUEUE_FILTERS,
            "selected_status": queue_status,
            "page": page,
            "total_pages": total_pages,
            "total_rows": total_rows,
        },
    )


@router.post("/documents")
async def upload_document(
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    try:
        document = await DocumentApplicationService(session).upload(
            original_file_name=file.filename or "upload",
            content_type=file.content_type,
            chunks=iter_upload_file(file),
        )
    except DocumentValidationError as exc:
        raise map_validation_error(exc) from None
    await session.commit()
    return RedirectResponse(
        f"/admin/documents/{document.id}?message=uploaded", status_code=303
    )


@router.get("/documents/{document_id}")
async def document_detail(
    request: Request,
    document_id: int,
    session: AsyncSession = Depends(get_db_session),
):
    document = await session.scalar(
        select(SourceDocument)
        .where(
            SourceDocument.id == document_id,
            SourceDocument.is_deleted.is_(False),
        )
        .options(
            selectinload(SourceDocument.extraction_runs).selectinload(
                ProfileExtractionRun.candidate
            )
        )
    )
    if document is None:
        raise DomainError("not_found", f"SourceDocument {document_id} not found", 404)
    runs = sorted(document.extraction_runs, key=lambda run: run.id, reverse=True)
    return _render_admin_template(
        request,
        "admin/document_detail.html",
        {
            "document": document,
            "runs": runs,
            "message": request.query_params.get("message"),
        },
    )


@router.get("/documents/{document_id}/content")
async def document_content(
    document_id: int, session: AsyncSession = Depends(get_db_session)
) -> FileResponse:
    document = await session.get(SourceDocument, document_id)
    if document is None or document.is_deleted:
        raise DomainError("not_found", f"SourceDocument {document_id} not found", 404)
    path = FileSystemDocumentStorage(Settings().document_storage_root).resolve_path(
        document.storage_uri
    )
    return FileResponse(
        path,
        media_type=document.media_type,
        filename=document.original_file_name,
        content_disposition_type="inline",
    )


@router.post("/documents/{document_id}/extract")
async def extract_document(
    document_id: int, session: AsyncSession = Depends(get_db_session)
) -> RedirectResponse:
    run = await DocumentApplicationService(session).queue_extraction(document_id)
    await session.commit()
    dispatch_extraction_task(run.id)
    return RedirectResponse(
        f"/admin/documents/{document_id}?message=extraction-queued", status_code=303
    )


@router.get("/candidates/{candidate_id}")
async def candidate_review(
    request: Request,
    candidate_id: int,
    session: AsyncSession = Depends(get_db_session),
):
    candidate = await _get_candidate(session, candidate_id)
    chunks = (
        await session.scalars(
            select(DocumentChunk)
            .where(DocumentChunk.extraction_run_id == candidate.extraction_run_id)
            .order_by(DocumentChunk.block_index, DocumentChunk.id)
        )
    ).all()
    referenced = _evidence_refs(candidate.reviewed_payload)
    evidence = [chunk for chunk in chunks if chunk.stable_ref in referenced]
    missing_refs = sorted(referenced - {chunk.stable_ref for chunk in chunks})
    review_events = list(
        (
            await session.scalars(
                select(CandidateReviewEvent)
                .where(CandidateReviewEvent.candidate_id == candidate.id)
                .order_by(
                    CandidateReviewEvent.created_at.desc(),
                    CandidateReviewEvent.id.desc(),
                )
            )
        ).all()
    )
    return _render_admin_template(
        request,
        "admin/candidate_review.html",
        {
            "candidate": candidate,
            "document": candidate.extraction_run.source_document,
            "payload_json": json.dumps(
                candidate.reviewed_payload, ensure_ascii=False, indent=2
            ),
            "review_sections": present_candidate(
                candidate.original_payload, candidate.reviewed_payload
            ),
            "unresolved_suggestions": candidate.reviewed_payload.get(
                "unresolved_suggestions", []
            ),
            "evidence": evidence,
            "evidence_by_ref": {chunk.stable_ref: chunk for chunk in chunks},
            "missing_refs": missing_refs,
            "review_events": review_events,
            "message": request.query_params.get("message"),
        },
    )


@router.post("/candidates/{candidate_id}/save")
async def save_candidate(
    candidate_id: int,
    revision: int = Form(...),
    payload_json: str = Form(...),
    actor: str = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        raise DomainError(
            "candidate_json_invalid", f"Invalid JSON: {exc.msg}", 422
        ) from None
    if not isinstance(payload, dict):
        raise DomainError("candidate_json_invalid", "Candidate must be an object", 422)
    await DocumentService(session).update_candidate(
        candidate_id, revision=revision, payload=payload, actor=actor
    )
    await session.commit()
    return RedirectResponse(_candidate_url(candidate_id, "saved"), status_code=303)


@router.post("/candidates/{candidate_id}/save-fields")
async def save_candidate_fields(
    request: Request,
    candidate_id: int,
    actor: str = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    form = await request.form()
    try:
        revision = int(str(form.get("revision", "")))
    except ValueError:
        raise DomainError("candidate_revision_invalid", "Invalid revision", 422) from None
    changes = {
        key.removeprefix("field:"): str(value)
        for key, value in form.multi_items()
        if key.startswith("field:")
    }
    if not changes:
        raise DomainError(
            "candidate_fields_missing", "No candidate fields were submitted", 422
        )
    await DocumentService(session).update_candidate_fields(
        candidate_id, revision=revision, changes=changes, actor=actor
    )
    await session.commit()
    return RedirectResponse(_candidate_url(candidate_id, "saved"), status_code=303)


@router.post("/candidates/{candidate_id}/validate")
async def validate_candidate(
    candidate_id: int, session: AsyncSession = Depends(get_db_session)
) -> RedirectResponse:
    await DocumentService(session).validate_candidate(candidate_id)
    await session.commit()
    return RedirectResponse(_candidate_url(candidate_id, "validated"), status_code=303)


@router.post("/candidates/{candidate_id}/approve")
async def approve_candidate(
    candidate_id: int,
    revision: int = Form(...),
    actor: str = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    await DocumentService(session).approve_candidate(
        candidate_id, revision=revision, actor=actor
    )
    await session.commit()
    return RedirectResponse(_candidate_url(candidate_id, "approved"), status_code=303)


@router.post("/candidates/{candidate_id}/reject")
async def reject_candidate(
    candidate_id: int,
    revision: int = Form(...),
    note: str = Form(default=""),
    actor: str = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    await DocumentService(session).reject_candidate(
        candidate_id, revision=revision, note=note, actor=actor
    )
    await session.commit()
    return RedirectResponse(_candidate_url(candidate_id, "rejected"), status_code=303)


@router.post("/candidates/{candidate_id}/import")
async def import_candidate(
    candidate_id: int,
    actor: str = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    await DocumentService(session).import_candidate(candidate_id, actor=actor)
    await session.commit()
    return RedirectResponse(_candidate_url(candidate_id, "imported"), status_code=303)


@router.post("/candidates/{candidate_id}/reopen")
async def reopen_candidate(
    candidate_id: int,
    revision: int = Form(...),
    actor: str = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    await DocumentService(session).reopen_candidate(
        candidate_id, revision=revision, actor=actor
    )
    await session.commit()
    return RedirectResponse(_candidate_url(candidate_id, "reopened"), status_code=303)
