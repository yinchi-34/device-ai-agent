from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class DiagnosisInput:
    device_id: int
    symptom: str
    target_point_code: str | None = None
    max_steps: int = 8


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class AgentDecision:
    tool_name: str
    arguments: dict[str, Any]
    rationale: str = ""
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DiagnosisStep:
    sequence_number: int
    tool_name: str
    arguments: dict[str, Any]
    rationale: str
    result: dict[str, Any]
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DiagnosisOutcome:
    status: str
    report: dict[str, Any]
    steps: list[DiagnosisStep]


class DiagnosisModelProvider(Protocol):
    async def decide(
        self,
        *,
        diagnosis: DiagnosisInput,
        steps: Sequence[DiagnosisStep],
        tools: Sequence[ToolDefinition],
    ) -> AgentDecision:
        raise NotImplementedError


class DiagnosisToolbox(Protocol):
    def definitions(self) -> Sequence[ToolDefinition]:
        raise NotImplementedError

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        steps: Sequence[DiagnosisStep],
    ) -> dict[str, Any]:
        raise NotImplementedError


StepCallback = Callable[[DiagnosisStep], Awaitable[None]]
