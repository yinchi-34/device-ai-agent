from dataclasses import dataclass

from app.modules.diagnosis.agent import DiagnosisAgent
from app.modules.diagnosis.contracts import AgentDecision, DiagnosisInput


@dataclass
class OneDecisionProvider:
    decision: AgentDecision

    async def decide(self, *, diagnosis, steps, tools):
        return self.decision


class EmptyToolbox:
    def definitions(self):
        return []

    async def execute(self, name, arguments, steps):
        raise AssertionError("no executable tool should run")


async def test_terminal_completion_must_match_backend_schema():
    provider = OneDecisionProvider(
        AgentDecision(
            tool_name="finish_diagnosis",
            arguments={
                "summary": "Missing classification and confidence",
                "evidence_step_numbers": [],
            },
        )
    )

    outcome = await DiagnosisAgent(provider, EmptyToolbox()).run(
        DiagnosisInput(device_id=1, symptom="offline", max_steps=2)
    )

    assert outcome.status == "escalated"
    assert outcome.report["reason"] == "terminal_arguments_invalid"


async def test_terminal_completion_requires_at_least_one_evidence_step():
    provider = OneDecisionProvider(
        AgentDecision(
            tool_name="finish_diagnosis",
            arguments={
                "classification": "unknown",
                "summary": "no evidence",
                "confidence": 0.99,
                "evidence_step_numbers": [],
            },
        )
    )

    outcome = await DiagnosisAgent(provider, EmptyToolbox()).run(
        DiagnosisInput(device_id=1, symptom="offline", max_steps=2)
    )

    assert outcome.status == "escalated"
    assert outcome.report["reason"] == "terminal_arguments_invalid"
