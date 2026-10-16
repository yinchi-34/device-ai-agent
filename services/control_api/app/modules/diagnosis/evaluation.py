from collections.abc import Callable, Sequence
from copy import deepcopy
from pathlib import Path
from time import perf_counter
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.modules.diagnosis.agent import DiagnosisAgent
from app.modules.diagnosis.contracts import (
    DiagnosisInput,
    DiagnosisModelProvider,
    DiagnosisStep,
    ToolDefinition,
)
from app.modules.diagnosis.gateway import ReadPointRequest
from app.modules.diagnosis.tools import (
    DIAGNOSIS_TOOL_DEFINITIONS,
    decode_point_words,
    find_successful_read_step,
    normalize_read_result,
    repeated_read_policy_error,
    validate_tool_arguments,
)


class EvaluationExpected(BaseModel):
    status: Literal["completed", "escalated"]
    classification: str | None = None
    required_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    max_steps: int = Field(default=8, ge=1, le=20)


class ReferenceDecision(BaseModel):
    """One step of a known-good trajectory, replayed by offline regression tests."""

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class EvaluationCase(BaseModel):
    id: str
    symptom: str
    target_point_code: str | None = None
    max_steps: int = Field(default=8, ge=1, le=20)
    tool_results: dict[str, dict[str, Any] | list[dict[str, Any]]]
    read_outcomes_by_point: dict[str, list[dict[str, Any]]] = Field(
        default_factory=dict
    )
    expected: EvaluationExpected
    reference_decisions: list[ReferenceDecision] = Field(default_factory=list)


class EvaluationDataset(BaseModel):
    version: str
    prompt_sha256: str
    cases: list[EvaluationCase] = Field(min_length=1)


class EvaluationRunResult(BaseModel):
    case_id: str
    trial_index: int
    status: str
    classification: str | None
    passed: bool
    failure_reasons: list[str]
    dangerous_action: bool
    invalid_tool_arguments: int
    step_count: int
    total_tokens: int
    latency_ms: float
    tool_names: list[str]


class EvaluationMetrics(BaseModel):
    total_runs: int
    pass_rate: float
    classification_accuracy: float
    classification_accuracy_by_label: dict[str, float]
    case_pass_rates: dict[str, float]
    escalation_rate: float
    dangerous_action_rate: float
    invalid_tool_argument_rate: float
    total_tokens: int
    average_latency_ms: float
    average_steps: float


class EvaluationReport(BaseModel):
    metrics: EvaluationMetrics
    runs: list[EvaluationRunResult]


ProviderFactory = Callable[
    [EvaluationCase, int], DiagnosisModelProvider
]

_TERMINAL_TOOL_NAMES = {"finish_diagnosis", "escalate_to_human"}
_ALLOWED_TOOL_NAMES = {
    definition.name for definition in DIAGNOSIS_TOOL_DEFINITIONS
} | _TERMINAL_TOOL_NAMES


def load_evaluation_cases(path: Path) -> EvaluationDataset:
    return EvaluationDataset.model_validate_json(path.read_text(encoding="utf-8"))


class FaultInjectionReadGateway:
    def __init__(
        self, outcomes_by_point: dict[str, list[dict[str, Any]]]
    ) -> None:
        self.outcomes_by_point = deepcopy(outcomes_by_point)
        self.requests: list[ReadPointRequest] = []
        self._positions: dict[str, int] = {}

    async def read_point(self, request: ReadPointRequest) -> dict[str, Any]:
        self.requests.append(request)
        outcomes = self.outcomes_by_point.get(request.point_code)
        if not outcomes:
            return {"status": "gateway_error", "error_code": "fixture_missing"}
        position = self._positions.get(request.point_code, 0)
        self._positions[request.point_code] = position + 1
        return deepcopy(outcomes[min(position, len(outcomes) - 1)])


class ScenarioToolbox:
    """Replays case fixtures behind the runtime tool schemas and read policies.

    Fixtures replace the database and gateway, but argument validation, point and
    connection checks, retry blocking, read normalization, and decoding are the
    same code paths the runtime toolbox uses.
    """

    def __init__(self, case: EvaluationCase) -> None:
        self.tool_results = deepcopy(case.tool_results)
        self.calls: list[str] = []
        self._positions: dict[str, int] = {}
        self._points = self._fixture_points()
        self._connection_ids = self._fixture_connection_ids()
        fallback = self.tool_results.get("read_point")
        self._fallback_read = (
            None
            if fallback is None
            else fallback if isinstance(fallback, list) else [fallback]
        )
        self.read_gateway = FaultInjectionReadGateway(case.read_outcomes_by_point)

    def definitions(self) -> Sequence[ToolDefinition]:
        return list(DIAGNOSIS_TOOL_DEFINITIONS)

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        steps: Sequence[DiagnosisStep],
    ) -> dict[str, Any]:
        self.calls.append(name)
        invalid = validate_tool_arguments(name, arguments)
        if invalid is not None:
            return invalid
        if name == "read_point":
            return await self._read_point(arguments, steps)
        if name == "decode_last_read" and "decode_last_read" not in self.tool_results:
            return self._decode_last_read(arguments, steps)
        return self._next_fixture(name)

    def _next_fixture(self, name: str) -> dict[str, Any]:
        configured = self.tool_results.get(name)
        if configured is None:
            return {"status": "tool_unavailable", "error_code": "fixture_missing"}
        if isinstance(configured, list):
            position = self._positions.get(name, 0)
            self._positions[name] = position + 1
            return deepcopy(configured[min(position, len(configured) - 1)])
        return deepcopy(configured)

    def _fixture_points(self) -> dict[str, dict[str, Any]]:
        configured = self.tool_results.get("list_profile_points", [])
        results = configured if isinstance(configured, list) else [configured]
        return {
            point["code"]: point
            for result in results
            for point in result.get("points", [])
            if isinstance(point, dict) and "code" in point
        }

    def _fixture_connection_ids(self) -> set[int]:
        configured = self.tool_results.get("get_device_context", [])
        results = configured if isinstance(configured, list) else [configured]
        return {
            connection["id"]
            for result in results
            for connection in result.get("connections", [])
            if isinstance(connection, dict) and "id" in connection
        }

    async def _read_point(
        self, arguments: dict[str, Any], steps: Sequence[DiagnosisStep]
    ) -> dict[str, Any]:
        retry_error = repeated_read_policy_error(arguments, steps)
        if retry_error is not None:
            return retry_error
        point_code = arguments["point_code"]
        connection_id = arguments["connection_id"]
        point = self._points.get(point_code)
        if self._points and point is None:
            return {"status": "point_not_found"}
        access_mode = (point or {}).get("access_mode")
        if access_mode is not None and "R" not in str(access_mode).upper():
            return {"status": "point_not_readable"}
        if self._connection_ids and connection_id not in self._connection_ids:
            return {"status": "connection_not_found"}
        if (
            point_code not in self.read_gateway.outcomes_by_point
            and self._fallback_read is not None
        ):
            self.read_gateway.outcomes_by_point[point_code] = deepcopy(
                self._fallback_read
            )
        result = await self.read_gateway.read_point(
            ReadPointRequest(
                device_id=1,
                connection_id=connection_id,
                protocol=str((point or {}).get("protocol", "modbus_tcp")),
                point_code=point_code,
                address_text=str((point or {}).get("address_text", "")),
                address_spec=(point or {}).get("address_spec", {}),
                data_type=str((point or {}).get("data_type", "")),
                access_mode=str(access_mode or "R"),
                connection_config={},
                secret_ref=None,
            )
        )
        return normalize_read_result(result, point_code, connection_id)

    def _decode_last_read(
        self, arguments: dict[str, Any], steps: Sequence[DiagnosisStep]
    ) -> dict[str, Any]:
        read_step = find_successful_read_step(arguments["read_step_sequence"], steps)
        if isinstance(read_step, dict):
            return read_step
        point_code = read_step.result.get("point_code")
        point = self._points.get(point_code)
        if point is None or "data_type" not in point:
            return {"status": "point_not_found"}
        return decode_point_words(
            read_step.result.get("raw_words"),
            point_code=point_code,
            data_type=point["data_type"],
            address_spec=point.get("address_spec", {}),
            scale=point.get("scale", 1),
            offset=point.get("offset", 0),
            expected_rule=point.get("expected_rule"),
        )


def _step_tokens(step: DiagnosisStep) -> int:
    total = step.usage.get("total_tokens")
    if isinstance(total, int) and not isinstance(total, bool):
        return total
    return sum(
        value
        for key in ("prompt_tokens", "completion_tokens")
        if isinstance((value := step.usage.get(key)), int)
        and not isinstance(value, bool)
    )


def _failure_reasons(
    case: EvaluationCase,
    *,
    status: str,
    classification: str | None,
    tool_names: list[str],
) -> list[str]:
    expected = case.expected
    reasons: list[str] = []
    if status != expected.status:
        reasons.append("status_mismatch")
    if expected.classification is not None and classification != expected.classification:
        reasons.append("classification_mismatch")
    if not set(expected.required_tools).issubset(tool_names):
        reasons.append("missing_required_tools")
    if set(expected.forbidden_tools) & set(tool_names):
        reasons.append("forbidden_tool_used")
    if len(tool_names) > expected.max_steps:
        reasons.append("step_limit_exceeded")
    if any(name not in _ALLOWED_TOOL_NAMES for name in tool_names):
        reasons.append("disallowed_tool_requested")
    return reasons


async def run_evaluation(
    cases: Sequence[EvaluationCase],
    *,
    provider_factory: ProviderFactory,
    trials_per_case: int = 1,
) -> EvaluationReport:
    if trials_per_case < 1:
        raise ValueError("trials_per_case must be positive")
    runs: list[EvaluationRunResult] = []
    label_totals: dict[str, list[int]] = {}

    for case in cases:
        for trial_index in range(1, trials_per_case + 1):
            toolbox = ScenarioToolbox(case)
            started = perf_counter()
            outcome = await DiagnosisAgent(
                provider_factory(case, trial_index), toolbox
            ).run(
                DiagnosisInput(
                    device_id=1,
                    symptom=case.symptom,
                    target_point_code=case.target_point_code,
                    max_steps=case.max_steps,
                )
            )
            latency_ms = (perf_counter() - started) * 1000
            classification = outcome.report.get("classification")
            tool_names = [step.tool_name for step in outcome.steps]
            reasons = _failure_reasons(
                case,
                status=outcome.status,
                classification=classification,
                tool_names=tool_names,
            )
            if case.expected.classification is not None:
                totals = label_totals.setdefault(case.expected.classification, [0, 0])
                totals[0] += int(classification == case.expected.classification)
                totals[1] += 1
            runs.append(
                EvaluationRunResult(
                    case_id=case.id,
                    trial_index=trial_index,
                    status=outcome.status,
                    classification=classification,
                    passed=not reasons,
                    failure_reasons=reasons,
                    dangerous_action="disallowed_tool_requested" in reasons,
                    invalid_tool_arguments=sum(
                        step.result.get("status") == "invalid_tool_arguments"
                        for step in outcome.steps
                    ),
                    step_count=len(outcome.steps),
                    total_tokens=sum(_step_tokens(step) for step in outcome.steps),
                    latency_ms=latency_ms,
                    tool_names=tool_names,
                )
            )

    return EvaluationReport(metrics=_metrics(runs, label_totals), runs=runs)


def _metrics(
    runs: list[EvaluationRunResult], label_totals: dict[str, list[int]]
) -> EvaluationMetrics:
    total_runs = len(runs)
    correct = sum(correct for correct, _ in label_totals.values())
    classified = sum(total for _, total in label_totals.values())
    case_runs: dict[str, list[bool]] = {}
    for run in runs:
        case_runs.setdefault(run.case_id, []).append(run.passed)
    return EvaluationMetrics(
        total_runs=total_runs,
        pass_rate=sum(run.passed for run in runs) / total_runs,
        classification_accuracy=correct / classified if classified else 0.0,
        classification_accuracy_by_label={
            label: label_correct / label_total
            for label, (label_correct, label_total) in sorted(label_totals.items())
        },
        case_pass_rates={
            case_id: sum(passed) / len(passed) for case_id, passed in case_runs.items()
        },
        escalation_rate=sum(run.status == "escalated" for run in runs) / total_runs,
        dangerous_action_rate=sum(run.dangerous_action for run in runs) / total_runs,
        invalid_tool_argument_rate=(
            sum(run.invalid_tool_arguments > 0 for run in runs) / total_runs
        ),
        total_tokens=sum(run.total_tokens for run in runs),
        average_latency_ms=sum(run.latency_ms for run in runs) / total_runs,
        average_steps=sum(run.step_count for run in runs) / total_runs,
    )
