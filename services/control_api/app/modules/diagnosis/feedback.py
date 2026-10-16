from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.modules.diagnosis import repository
from app.modules.diagnosis.evaluation import (
    EvaluationCase,
    EvaluationExpected,
    ReferenceDecision,
)
from app.modules.diagnosis.models import DiagnosisFeedback, DiagnosisRun
from app.modules.diagnosis.tools import DIAGNOSIS_TOOL_DEFINITIONS

_TERMINAL_STATUSES = {"completed", "escalated"}
_REPLAYABLE_TOOLS = {definition.name for definition in DIAGNOSIS_TOOL_DEFINITIONS}
_TERMINAL_TOOLS = {"finish_diagnosis", "escalate_to_human"}
# Statuses produced by the gateway itself; everything else read_point returns is
# re-derived by the evaluation toolbox's policy checks.
_GATEWAY_READ_STATUSES = {
    "success",
    "timeout",
    "connection_refused",
    "modbus_exception",
    "gateway_error",
    "tool_unavailable",
}


class DiagnosisFeedbackService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _run(self, run_id: int) -> DiagnosisRun:
        run = await repository.get_run(self.session, run_id)
        if run is None:
            raise DomainError("not_found", f"DiagnosisRun {run_id} not found", 404)
        return run

    async def add_feedback(
        self,
        run_id: int,
        *,
        verdict: str,
        expected_status: str | None,
        expected_classification: str | None,
        reviewer: str,
        note: str | None,
    ) -> DiagnosisFeedback:
        run = await self._run(run_id)
        if run.status not in _TERMINAL_STATUSES:
            raise DomainError(
                "diagnosis_run_not_terminal",
                "Feedback is accepted only for completed or escalated runs",
                409,
            )
        if verdict == "correct":
            run_classification = run.final_report.get("classification")
            if (expected_status not in {None, run.status}) or (
                expected_classification not in {None, run_classification}
            ):
                raise DomainError(
                    "diagnosis_feedback_contradicts_run",
                    "A correct verdict cannot name a different outcome than the run",
                    422,
                )
            expected_status = run.status
            expected_classification = run_classification
        elif expected_status is None:
            raise DomainError(
                "diagnosis_feedback_status_required",
                "An incorrect verdict must state the expected status",
                422,
            )
        if expected_status == "completed" and expected_classification is None:
            raise DomainError(
                "diagnosis_feedback_classification_required",
                "A completed outcome needs the expected classification",
                422,
            )
        if expected_status == "escalated" and expected_classification is not None:
            raise DomainError(
                "diagnosis_feedback_classification_not_allowed",
                "An escalated outcome has no classification",
                422,
            )
        feedback = DiagnosisFeedback(
            diagnosis_run_id=run.id,
            verdict=verdict,
            expected_status=expected_status,
            expected_classification=expected_classification,
            reviewer=reviewer,
            note=note,
        )
        self.session.add(feedback)
        await self.session.flush()
        return feedback

    async def list_feedback(self, run_id: int) -> list[DiagnosisFeedback]:
        await self._run(run_id)
        return list(await repository.list_feedback(self.session, run_id))

    async def evaluation_case(self, run_id: int) -> EvaluationCase:
        run = await self._run(run_id)
        feedback = await repository.list_feedback(self.session, run_id)
        if not feedback:
            raise DomainError(
                "diagnosis_feedback_required",
                "Label the run with feedback before exporting it as an evaluation case",
                409,
            )
        return build_evaluation_case(run, feedback[-1])


def build_evaluation_case(
    run: DiagnosisRun, feedback: DiagnosisFeedback
) -> EvaluationCase:
    """Freeze a labelled run's observations into a replayable evaluation case."""
    tool_results: dict[str, list[dict[str, Any]]] = {}
    read_outcomes: dict[str, list[dict[str, Any]]] = {}
    for step in run.steps:
        if step.tool_name not in _REPLAYABLE_TOOLS:
            continue
        if step.tool_name == "read_point":
            if step.result.get("status") in _GATEWAY_READ_STATUSES:
                read_outcomes.setdefault(step.arguments["point_code"], []).append(
                    step.result
                )
            continue
        if step.result.get("status") == "invalid_tool_arguments":
            continue
        tool_results.setdefault(step.tool_name, []).append(step.result)

    reference: list[ReferenceDecision] = []
    if feedback.verdict == "correct":
        reference = [
            ReferenceDecision(tool_name=step.tool_name, arguments=step.arguments)
            for step in run.steps
            if step.tool_name in _REPLAYABLE_TOOLS | _TERMINAL_TOOLS
        ]
    return EvaluationCase(
        id=f"run-{run.id}-feedback-{feedback.id}",
        symptom=run.symptom,
        target_point_code=run.target_point_code,
        max_steps=run.max_steps,
        tool_results=tool_results,
        read_outcomes_by_point=read_outcomes,
        expected=EvaluationExpected(
            status=feedback.expected_status,
            classification=feedback.expected_classification,
            max_steps=run.max_steps,
        ),
        reference_decisions=reference,
    )
