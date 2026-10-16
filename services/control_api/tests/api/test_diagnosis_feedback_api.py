from pathlib import Path

from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.modules.diagnosis.contracts import AgentDecision
from app.modules.diagnosis.evaluation import EvaluationCase, run_evaluation
from app.modules.diagnosis.router import (
    build_diagnosis_provider,
    build_read_gateway,
)
from tests.api.test_diagnosis_api import (
    RecordingReadGateway,
    ScriptedProvider,
    _make_released_device,
)

# Harmless while /api/v1 is open; matches the test token once API auth lands.
_AUTH_HEADERS = {"Authorization": "Bearer test-api-token"}


def _decisions(connection_id: int) -> list[AgentDecision]:
    return [
        AgentDecision(tool_name="get_device_context", arguments={}),
        AgentDecision(
            tool_name="list_profile_points",
            arguments={"point_code": "inlet_pressure"},
        ),
        AgentDecision(
            tool_name="read_point",
            arguments={"connection_id": connection_id, "point_code": "inlet_pressure"},
        ),
        AgentDecision(
            tool_name="decode_last_read", arguments={"read_step_sequence": 3}
        ),
        AgentDecision(
            tool_name="finish_diagnosis",
            arguments={
                "classification": "encoding",
                "summary": "Published little word order decodes the value as 10.0.",
                "confidence": 0.95,
                "evidence_step_numbers": [3, 4],
            },
        ),
    ]


async def _executed_run(db_session, client_app):
    _, device, connection = await _make_released_device(db_session)
    client_app.dependency_overrides[build_diagnosis_provider] = lambda: (
        ScriptedProvider(_decisions(connection.id))
    )
    client_app.dependency_overrides[build_read_gateway] = lambda: (
        RecordingReadGateway({"status": "success", "raw_words": [0x0000, 0x4120]})
    )
    return device


def _client(app) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=_AUTH_HEADERS,
    )


async def _create_and_execute(client, device_id: int) -> dict:
    created = await client.post(
        "/api/v1/diagnosis-runs",
        json={
            "device_id": device_id,
            "symptom": "Parsed inlet pressure is clearly abnormal",
            "target_point_code": "inlet_pressure",
            "max_steps": 6,
        },
    )
    executed = await client.post(
        f"/api/v1/diagnosis-runs/{created.json()['id']}/execute"
    )
    assert executed.status_code == 200
    return executed.json()


async def test_feedback_confirms_run_and_exports_replayable_eval_case(db_session):
    app = create_app()
    device = await _executed_run(db_session, app)

    async with _client(app) as client:
        run = await _create_and_execute(client, device.id)
        feedback = await client.post(
            f"/api/v1/diagnosis-runs/{run['id']}/feedback",
            json={"verdict": "correct", "reviewer": "field-engineer", "note": "Fixed."},
        )
        listed = await client.get(f"/api/v1/diagnosis-runs/{run['id']}/feedback")
        exported = await client.get(
            f"/api/v1/diagnosis-runs/{run['id']}/evaluation-case"
        )

    assert feedback.status_code == 201
    body = feedback.json()
    assert body["verdict"] == "correct"
    assert body["expected_status"] == "completed"
    assert body["expected_classification"] == "encoding"
    assert body["reviewer"] == "field-engineer"
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [body["id"]]

    assert exported.status_code == 200
    case = EvaluationCase.model_validate(exported.json())
    assert case.id == f"run-{run['id']}-feedback-{body['id']}"
    assert case.symptom == "Parsed inlet pressure is clearly abnormal"
    assert case.expected.status == "completed"
    assert case.expected.classification == "encoding"
    assert case.read_outcomes_by_point["inlet_pressure"][0]["raw_words"] == [
        0x0000,
        0x4120,
    ]
    assert "read_point" not in case.tool_results
    assert [item.tool_name for item in case.reference_decisions] == [
        step["tool_name"] for step in run["steps"]
    ]

    class Replay:
        def __init__(self):
            self.decisions = [
                AgentDecision(tool_name=item.tool_name, arguments=item.arguments)
                for item in case.reference_decisions
            ]

        async def decide(self, *, diagnosis, steps, tools):
            return self.decisions.pop(0)

    report = await run_evaluation([case], provider_factory=lambda c, i: Replay())
    assert report.runs[0].failure_reasons == []


async def test_feedback_correcting_a_run_exports_case_without_reference(db_session):
    app = create_app()
    device = await _executed_run(db_session, app)

    async with _client(app) as client:
        run = await _create_and_execute(client, device.id)
        feedback = await client.post(
            f"/api/v1/diagnosis-runs/{run['id']}/feedback",
            json={
                "verdict": "incorrect",
                "expected_status": "completed",
                "expected_classification": "semantic",
                "reviewer": "field-engineer",
            },
        )
        exported = await client.get(
            f"/api/v1/diagnosis-runs/{run['id']}/evaluation-case"
        )

    assert feedback.status_code == 201
    case = EvaluationCase.model_validate(exported.json())
    assert case.expected.classification == "semantic"
    assert case.reference_decisions == []


async def test_feedback_validation_rules(db_session):
    app = create_app()
    device = await _executed_run(db_session, app)

    async with _client(app) as client:
        created = await client.post(
            "/api/v1/diagnosis-runs",
            json={"device_id": device.id, "symptom": "pending", "max_steps": 6},
        )
        not_terminal = await client.post(
            f"/api/v1/diagnosis-runs/{created.json()['id']}/feedback",
            json={"verdict": "correct", "reviewer": "qa"},
        )
        no_feedback_export = await client.get(
            f"/api/v1/diagnosis-runs/{created.json()['id']}/evaluation-case"
        )
        run = await _create_and_execute(client, device.id)
        contradicting = await client.post(
            f"/api/v1/diagnosis-runs/{run['id']}/feedback",
            json={
                "verdict": "correct",
                "expected_classification": "transport",
                "reviewer": "qa",
            },
        )
        incomplete = await client.post(
            f"/api/v1/diagnosis-runs/{run['id']}/feedback",
            json={"verdict": "incorrect", "expected_status": "completed", "reviewer": "qa"},
        )
        escalated_with_label = await client.post(
            f"/api/v1/diagnosis-runs/{run['id']}/feedback",
            json={
                "verdict": "incorrect",
                "expected_status": "escalated",
                "expected_classification": "transport",
                "reviewer": "qa",
            },
        )
        missing_run = await client.post(
            "/api/v1/diagnosis-runs/999999/feedback",
            json={"verdict": "correct", "reviewer": "qa"},
        )

    assert not_terminal.status_code == 409
    assert not_terminal.json()["code"] == "diagnosis_run_not_terminal"
    assert no_feedback_export.status_code == 409
    assert no_feedback_export.json()["code"] == "diagnosis_feedback_required"
    assert contradicting.status_code == 422
    assert contradicting.json()["code"] == "diagnosis_feedback_contradicts_run"
    assert incomplete.status_code == 422
    assert incomplete.json()["code"] == "diagnosis_feedback_classification_required"
    assert escalated_with_label.status_code == 422
    assert (
        escalated_with_label.json()["code"]
        == "diagnosis_feedback_classification_not_allowed"
    )
    assert missing_run.status_code == 404


def test_feedback_export_script_is_present():
    script = Path(__file__).parents[2] / "scripts" / "export_diagnosis_feedback_cases.py"

    assert script.is_file()
