from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.session import get_db_session
from app.modules.diagnosis import schemas
from app.modules.diagnosis.evaluation import EvaluationCase
from app.modules.diagnosis.feedback import DiagnosisFeedbackService
from app.modules.diagnosis.gateway import (
    HttpReadOnlyRegisterGateway,
    UnavailableReadGateway,
)
from app.modules.diagnosis.openai_compatible import OpenAICompatibleDiagnosisProvider
from app.modules.diagnosis.service import DiagnosisService
from app.modules.document.openai_compatible import OpenAICompatibleEmbeddingProvider

router = APIRouter(prefix="/api/v1", tags=["diagnosis"])


def build_diagnosis_provider() -> OpenAICompatibleDiagnosisProvider:
    settings = Settings()
    return OpenAICompatibleDiagnosisProvider(
        base_url=settings.diagnosis_llm_base_url,
        api_key=settings.diagnosis_llm_api_key,
        model=settings.diagnosis_llm_model,
        timeout_seconds=settings.diagnosis_llm_timeout_seconds,
    )


def build_read_gateway():
    settings = Settings()
    if not settings.diagnosis_read_gateway_url:
        return UnavailableReadGateway()
    return HttpReadOnlyRegisterGateway(
        base_url=settings.diagnosis_read_gateway_url,
        token=settings.diagnosis_read_gateway_token,
        timeout_seconds=settings.diagnosis_read_gateway_timeout_seconds,
    )


def build_diagnosis_embedding_provider():
    settings = Settings()
    if not settings.document_embedding_model:
        return None
    return OpenAICompatibleEmbeddingProvider(
        base_url=settings.document_embedding_base_url,
        api_key=settings.document_embedding_api_key,
        model=settings.document_embedding_model,
        timeout_seconds=settings.document_embedding_timeout_seconds,
    )


@router.post("/diagnosis-runs", status_code=201, response_model=schemas.DiagnosisRunOut)
async def create_diagnosis_run(
    payload: schemas.DiagnosisRunCreate, session: AsyncSession = Depends(get_db_session)
) -> object:
    settings = Settings()
    run = await DiagnosisService(session).create_run(
        **payload.model_dump(),
        model_provider="openai_compatible",
        model_name=settings.diagnosis_llm_model,
    )
    await session.commit()
    return run


@router.get("/diagnosis-runs/{run_id}", response_model=schemas.DiagnosisRunOut)
async def get_diagnosis_run(
    run_id: int, session: AsyncSession = Depends(get_db_session)
) -> object:
    return await DiagnosisService(session).get_run(run_id)


@router.post("/diagnosis-runs/{run_id}/execute", response_model=schemas.DiagnosisRunOut)
async def execute_diagnosis_run(
    run_id: int,
    session: AsyncSession = Depends(get_db_session),
    provider=Depends(build_diagnosis_provider),
    read_gateway=Depends(build_read_gateway),
    embedding_provider=Depends(build_diagnosis_embedding_provider),
) -> object:
    run = await DiagnosisService(session).execute(
        run_id,
        provider=provider,
        read_gateway=read_gateway,
        embedding_provider=embedding_provider,
    )
    await session.commit()
    return run


@router.post(
    "/diagnosis-runs/{run_id}/feedback",
    status_code=201,
    response_model=schemas.DiagnosisFeedbackOut,
)
async def create_diagnosis_feedback(
    run_id: int,
    payload: schemas.DiagnosisFeedbackCreate,
    session: AsyncSession = Depends(get_db_session),
) -> object:
    feedback = await DiagnosisFeedbackService(session).add_feedback(
        run_id, **payload.model_dump()
    )
    await session.commit()
    return feedback


@router.get(
    "/diagnosis-runs/{run_id}/feedback",
    response_model=list[schemas.DiagnosisFeedbackOut],
)
async def list_diagnosis_feedback(
    run_id: int, session: AsyncSession = Depends(get_db_session)
) -> object:
    return await DiagnosisFeedbackService(session).list_feedback(run_id)


@router.get(
    "/diagnosis-runs/{run_id}/evaluation-case", response_model=EvaluationCase
)
async def export_diagnosis_evaluation_case(
    run_id: int, session: AsyncSession = Depends(get_db_session)
) -> object:
    return await DiagnosisFeedbackService(session).evaluation_case(run_id)
