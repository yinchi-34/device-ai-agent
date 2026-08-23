from dataclasses import dataclass, field
from typing import Any

from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.modules.device.service import DeviceService
from app.modules.diagnosis.contracts import AgentDecision
from app.modules.diagnosis.router import (
    build_diagnosis_provider,
    build_read_gateway,
)
from app.modules.testing.service import TestingService as DeviceTestingService
from tests.device.helpers import make_profile_graph


@dataclass
class ScriptedProvider:
    decisions: list[AgentDecision]
    calls: list[list[dict[str, Any]]] = field(default_factory=list)

    async def decide(self, *, diagnosis, steps, tools):
        self.calls.append([step.result for step in steps])
        return self.decisions.pop(0)


@dataclass
class RecordingReadGateway:
    result: dict[str, Any]
    requests: list[Any] = field(default_factory=list)

    async def read_point(self, request):
        self.requests.append(request)
        return self.result


async def _make_released_device(db_session):
    graph = await make_profile_graph(db_session)
    point = graph["points"]["inlet_pressure"]
    point.address_spec = {"word_order": "little", "byte_order": "big"}
    point.expected_rule = {"minimum": 1, "maximum": 20}
    await DeviceService(db_session).release_profile(graph["profile"].id)
    testing = DeviceTestingService(db_session)
    device = await testing.create_instance(
        device_model_id=graph["model"].id,
        profile_version_id=graph["profile"].id,
        instance_code="PUMP-DIAG-001",
        instance_name="Diagnosis pump",
    )
    connection = await testing.create_connection(
        device_id=device.id,
        protocol_spec_id=graph["spec"].id,
        name="fieldbus",
        config={"host": "10.0.0.5", "port": 502},
        secret_ref="vault://modbus/unit-id",
    )
    await db_session.commit()
    return graph, device, connection


async def test_diagnosis_agent_executes_read_only_feedback_loop_and_persists_audit(
    db_session,
):
    graph, device, connection = await _make_released_device(db_session)
    provider = ScriptedProvider(
        [
            AgentDecision(tool_name="get_device_context", arguments={}),
            AgentDecision(
                tool_name="list_profile_points",
                arguments={"point_code": "inlet_pressure"},
            ),
            AgentDecision(
                tool_name="read_point",
                arguments={
                    "connection_id": connection.id,
                    "point_code": "inlet_pressure",
                },
            ),
            AgentDecision(
                tool_name="decode_last_read",
                arguments={"read_step_sequence": 3},
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
    )
    gateway = RecordingReadGateway({"status": "success", "raw_words": [0x0000, 0x4120]})
    app = create_app()
    app.dependency_overrides[build_diagnosis_provider] = lambda: provider
    app.dependency_overrides[build_read_gateway] = lambda: gateway

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/api/v1/diagnosis-runs",
            json={
                "device_id": device.id,
                "symptom": "Parsed inlet pressure is clearly abnormal",
                "target_point_code": "inlet_pressure",
                "max_steps": 6,
            },
        )
        assert created.status_code == 201
        assert created.json()["status"] == "ready"

        executed = await client.post(
            f"/api/v1/diagnosis-runs/{created.json()['id']}/execute"
        )
        assert executed.status_code == 200
        body = executed.json()
        assert body["status"] == "completed"
        assert body["final_report"]["classification"] == "encoding"
        assert [step["tool_name"] for step in body["steps"]] == [
            "get_device_context",
            "list_profile_points",
            "read_point",
            "decode_last_read",
            "finish_diagnosis",
        ]
        assert body["steps"][3]["result"]["engineering_value"] == 10.0

        fetched = await client.get(f"/api/v1/diagnosis-runs/{created.json()['id']}")
        assert fetched.status_code == 200
        assert fetched.json() == body

    assert len(gateway.requests) == 1
    request = gateway.requests[0]
    assert request.device_id == device.id
    assert request.point_code == "inlet_pressure"
    assert request.address_text == graph["points"]["inlet_pressure"].address_text
    assert request.access_mode == "R"
    # Secrets are available only to the gateway adapter, never model feedback.
    assert "secret_ref" not in provider.calls[1][0]["connections"][0]


async def test_diagnosis_run_rejects_unknown_target_point(db_session):
    _, device, _ = await _make_released_device(db_session)
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/diagnosis-runs",
            json={
                "device_id": device.id,
                "symptom": "bad value",
                "target_point_code": "not_in_released_profile",
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == "diagnosis_target_point_not_found"
