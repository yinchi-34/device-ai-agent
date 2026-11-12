from app.modules.diagnosis.gateway import UnavailableReadGateway
from app.modules.diagnosis.resilience import ModelProviderError
from app.modules.diagnosis.service import DiagnosisService
from app.modules.diagnosis.tools import RuntimeDiagnosisToolbox
from app.modules.document.providers import ProviderError
from tests.api.test_diagnosis_api import _make_released_device
from tests.document.test_document_retrieval import make_document_with_chunk


class RaisingProvider:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    async def decide(self, *, diagnosis, steps, tools, memory=None):
        raise self.exc


class FailingEmbeddingProvider:
    async def embed(self, texts):
        raise ProviderError("provider_timeout", retryable=True)


async def _create_run(db_session):
    graph, device, _ = await _make_released_device(db_session)
    run = await DiagnosisService(db_session).create_run(
        device_id=device.id,
        symptom="pressure alarm",
        target_point_code=None,
        max_steps=4,
        model_provider="fake",
        model_name="fake",
    )
    await db_session.commit()
    return graph, run


async def test_exhausted_model_routes_fail_the_run_with_a_classified_cause(db_session):
    _, run = await _create_run(db_session)
    error = ModelProviderError(
        "diagnosis_model_unavailable",
        retryable=False,
        detail={"attempts": [{"route": "primary", "error_code": "model_timeout"}]},
    )

    result = await DiagnosisService(db_session).execute(
        run.id, provider=RaisingProvider(error), read_gateway=UnavailableReadGateway()
    )

    assert result.status == "failed"
    assert result.error_code == "diagnosis_execution_failed"
    assert result.error_detail["cause"] == "diagnosis_model_unavailable"
    assert result.error_detail["attempts"][0]["error_code"] == "model_timeout"


async def test_unexpected_fault_never_leaves_a_run_stuck_in_running(db_session):
    _, run = await _create_run(db_session)

    result = await DiagnosisService(db_session).execute(
        run.id,
        provider=RaisingProvider(ZeroDivisionError()),
        read_gateway=UnavailableReadGateway(),
    )

    assert result.status == "failed"
    assert result.error_detail == {
        "type": "ZeroDivisionError",
        "cause": "internal_error",
    }


async def _manual_for(db_session, graph):
    document = await make_document_with_chunk(
        db_session,
        "manual-p12-b3",
        "Overpressure alarm triggers when inlet pressure exceeds 16 bar.",
        [0.1, 0.2, 0.3],
    )
    document.device_model_id = graph["model"].id
    await db_session.flush()


async def test_manual_search_degrades_to_keywords_when_embeddings_fail(db_session):
    graph, run = await _create_run(db_session)
    await _manual_for(db_session, graph)
    toolbox = RuntimeDiagnosisToolbox(
        db_session,
        run,
        read_gateway=UnavailableReadGateway(),
        embedding_provider=FailingEmbeddingProvider(),
    )

    result = await toolbox.execute(
        "search_manual", {"query": "overpressure alarm threshold"}, []
    )

    assert result["retrieval_mode"] == "keyword"
    assert result["degraded_reason"] == "provider_timeout"
    assert [item["stable_ref"] for item in result["untrusted_manual_evidence"]] == [
        "manual-p12-b3"
    ]


async def test_manual_search_without_embeddings_still_returns_evidence(db_session):
    graph, run = await _create_run(db_session)
    await _manual_for(db_session, graph)
    toolbox = RuntimeDiagnosisToolbox(
        db_session, run, read_gateway=UnavailableReadGateway()
    )

    result = await toolbox.execute("search_manual", {"query": "inlet pressure"}, [])

    assert result["degraded_reason"] == "embedding_provider_not_configured"
    assert len(result["untrusted_manual_evidence"]) == 1
