from dataclasses import dataclass, field
from typing import Any

from app.modules.diagnosis.agent import DiagnosisAgent
from app.modules.diagnosis.contracts import (
    AgentDecision,
    DiagnosisInput,
    ToolDefinition,
)


@dataclass
class ScriptedProvider:
    decisions: list[AgentDecision]
    calls: list[list[dict[str, Any]]] = field(default_factory=list)

    async def decide(self, *, diagnosis, steps, tools):
        self.calls.append([step.result for step in steps])
        return self.decisions.pop(0)


@dataclass
class FakeToolbox:
    results: dict[str, dict[str, Any]]
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def definitions(self):
        return [
            ToolDefinition(name=name, description=name, parameters={"type": "object"})
            for name in self.results
        ]

    async def execute(self, name, arguments, steps):
        self.calls.append((name, arguments))
        return self.results[name]


async def test_agent_uses_tool_feedback_and_finishes_with_audit_steps():
    provider = ScriptedProvider(
        [
            AgentDecision(tool_name="get_device_context", arguments={}),
            AgentDecision(
                tool_name="read_point", arguments={"point_code": "temperature"}
            ),
            AgentDecision(
                tool_name="finish_diagnosis",
                arguments={
                    "classification": "transport",
                    "summary": "The target refused the TCP connection.",
                    "confidence": 0.98,
                    "evidence_step_numbers": [2],
                },
            ),
        ]
    )
    toolbox = FakeToolbox(
        {
            "get_device_context": {"device": {"id": 7}},
            "read_point": {"status": "connection_refused"},
        }
    )

    outcome = await DiagnosisAgent(provider, toolbox).run(
        DiagnosisInput(device_id=7, symptom="device offline", max_steps=5)
    )

    assert outcome.status == "completed"
    assert outcome.report["classification"] == "transport"
    assert [step.sequence_number for step in outcome.steps] == [1, 2, 3]
    assert provider.calls[2][1]["status"] == "connection_refused"
    assert toolbox.calls == [
        ("get_device_context", {}),
        ("read_point", {"point_code": "temperature"}),
    ]


async def test_agent_escalates_when_model_requests_non_allowlisted_write_tool():
    provider = ScriptedProvider(
        [AgentDecision(tool_name="write_register", arguments={"address": 1})]
    )
    toolbox = FakeToolbox({"get_device_context": {}})

    outcome = await DiagnosisAgent(provider, toolbox).run(
        DiagnosisInput(device_id=7, symptom="offline", max_steps=5)
    )

    assert outcome.status == "escalated"
    assert outcome.report["reason"] == "tool_not_allowed"
    assert outcome.steps[0].result["requested_tool"] == "write_register"
    assert toolbox.calls == []


async def test_backend_step_budget_is_hard_even_if_model_keeps_requesting_tools():
    provider = ScriptedProvider(
        [
            AgentDecision(tool_name="get_device_context", arguments={}),
            AgentDecision(tool_name="get_device_context", arguments={}),
            AgentDecision(tool_name="get_device_context", arguments={}),
        ]
    )
    toolbox = FakeToolbox({"get_device_context": {"ok": True}})

    outcome = await DiagnosisAgent(provider, toolbox).run(
        DiagnosisInput(device_id=7, symptom="offline", max_steps=2)
    )

    assert outcome.status == "escalated"
    assert outcome.report["reason"] == "step_budget_exhausted"
    assert len(outcome.steps) == 2
    assert len(toolbox.calls) == 2


async def test_finish_rejects_evidence_that_is_not_in_the_audit_trail():
    provider = ScriptedProvider(
        [
            AgentDecision(
                tool_name="finish_diagnosis",
                arguments={
                    "classification": "unknown",
                    "summary": "Unsupported conclusion",
                    "confidence": 0.9,
                    "evidence_step_numbers": [99],
                },
            )
        ]
    )
    toolbox = FakeToolbox({})

    outcome = await DiagnosisAgent(provider, toolbox).run(
        DiagnosisInput(device_id=7, symptom="offline", max_steps=2)
    )

    assert outcome.status == "escalated"
    assert outcome.report["reason"] == "invalid_evidence_reference"
