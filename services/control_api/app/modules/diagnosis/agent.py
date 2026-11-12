from collections.abc import Awaitable, Callable, Sequence

from jsonschema import Draft202012Validator

from app.modules.diagnosis.contracts import (
    DiagnosisInput,
    DiagnosisModelProvider,
    DiagnosisOutcome,
    DiagnosisStep,
    DiagnosisToolbox,
    StepCallback,
    ToolDefinition,
    WorkingMemory,
)
from app.modules.diagnosis.grounding import check_grounding
from app.modules.diagnosis.memory import advance_memory, build_memory

_FINISH_TOOL = ToolDefinition(
    name="finish_diagnosis",
    description="Finish with an evidence-backed diagnosis.",
    parameters={
        "type": "object",
        "additionalProperties": False,
        "required": [
            "classification",
            "summary",
            "confidence",
            "evidence_step_numbers",
        ],
        "properties": {
            "classification": {
                "type": "string",
                "enum": [
                    "transport",
                    "protocol",
                    "encoding",
                    "semantic",
                    "healthy",
                    "unknown",
                ],
            },
            "summary": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "evidence_step_numbers": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "integer", "minimum": 1},
            },
            "cited_values": {
                "type": "array",
                "description": "Every observed value the summary relies on, "
                "with the audit step it was observed in.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["step", "value"],
                    "properties": {
                        "step": {"type": "integer", "minimum": 1},
                        "value": {"type": ["number", "string"]},
                    },
                },
            },
            "manual_citations": {
                "type": "array",
                "description": "Verbatim manual quotes the summary relies on.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["step", "stable_ref", "quote"],
                    "properties": {
                        "step": {"type": "integer", "minimum": 1},
                        "stable_ref": {"type": "string", "minLength": 1},
                        "quote": {"type": "string", "minLength": 1, "maxLength": 300},
                    },
                },
            },
        },
    },
)

_ESCALATE_TOOL = ToolDefinition(
    name="escalate_to_human",
    description="Stop when safe autonomous diagnosis cannot continue.",
    parameters={
        "type": "object",
        "additionalProperties": False,
        "required": ["reason", "required_action", "evidence_step_numbers"],
        "properties": {
            "reason": {"type": "string"},
            "required_action": {"type": "string"},
            "evidence_step_numbers": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "integer", "minimum": 1},
            },
        },
    },
)


class DiagnosisAgent:
    """A bounded model -> tool -> observation feedback loop."""

    def __init__(
        self,
        provider: DiagnosisModelProvider,
        toolbox: DiagnosisToolbox,
        *,
        min_confidence: float = 0.5,
    ) -> None:
        self.provider = provider
        self.toolbox = toolbox
        self.min_confidence = min_confidence

    async def run(
        self,
        diagnosis: DiagnosisInput,
        *,
        existing_steps: Sequence[DiagnosisStep] = (),
        on_step: StepCallback | None = None,
        initial_memory: WorkingMemory | None = None,
    ) -> DiagnosisOutcome:
        if not 1 <= diagnosis.max_steps <= 20:
            raise ValueError("max_steps must be between 1 and 20")

        steps = list(existing_steps)
        memory = initial_memory
        if memory is None or memory.as_of_step != len(steps):
            # A stale or missing snapshot is never trusted; rebuild from the trail.
            memory = build_memory(steps)

        async def append(step: DiagnosisStep) -> None:
            nonlocal memory
            steps.append(step)
            memory = advance_memory(memory, step)
            if on_step is not None:
                await on_step(step, memory)
        tool_definitions = list(self.toolbox.definitions())
        executable_names = {tool.name for tool in tool_definitions}
        model_tools = [*tool_definitions, _FINISH_TOOL, _ESCALATE_TOOL]

        while len(steps) < diagnosis.max_steps:
            decision = await self.provider.decide(
                diagnosis=diagnosis,
                steps=steps,
                tools=model_tools,
                memory=memory,
            )
            sequence_number = len(steps) + 1

            if decision.tool_name == "finish_diagnosis":
                if not Draft202012Validator(_FINISH_TOOL.parameters).is_valid(
                    decision.arguments
                ):
                    return await self._terminal_policy_violation(
                        steps,
                        sequence_number,
                        decision.tool_name,
                        decision.arguments,
                        decision.rationale,
                        decision.usage,
                        "terminal_arguments_invalid",
                        append,
                    )
                if not _valid_evidence_references(
                    decision.arguments, completed_step_count=len(steps)
                ):
                    return await self._terminal_policy_violation(
                        steps,
                        sequence_number,
                        decision.tool_name,
                        decision.arguments,
                        decision.rationale,
                        decision.usage,
                        "invalid_evidence_reference",
                        append,
                    )
                grounding = check_grounding(
                    decision.arguments,
                    steps,
                    symptom=diagnosis.symptom,
                    min_confidence=self.min_confidence,
                )
                if not grounding.grounded:
                    return await self._terminal_policy_violation(
                        steps,
                        sequence_number,
                        decision.tool_name,
                        decision.arguments,
                        decision.rationale,
                        decision.usage,
                        "evidence_grounding_failed",
                        append,
                        violations=grounding.violations,
                        evidence_step_numbers=decision.arguments[
                            "evidence_step_numbers"
                        ],
                    )
                step = DiagnosisStep(
                    sequence_number=sequence_number,
                    tool_name=decision.tool_name,
                    arguments=decision.arguments,
                    rationale=decision.rationale,
                    result={"status": "completed"},
                    usage=decision.usage,
                )
                await append(step)
                report = {
                    **decision.arguments,
                    "grounding": {
                        "checked": True,
                        "unverified_numbers": grounding.unverified_numbers,
                    },
                }
                return DiagnosisOutcome("completed", report, steps)

            if decision.tool_name == "escalate_to_human":
                if not Draft202012Validator(_ESCALATE_TOOL.parameters).is_valid(
                    decision.arguments
                ):
                    return await self._terminal_policy_violation(
                        steps,
                        sequence_number,
                        decision.tool_name,
                        decision.arguments,
                        decision.rationale,
                        decision.usage,
                        "terminal_arguments_invalid",
                        append,
                    )
                if not _valid_evidence_references(
                    decision.arguments, completed_step_count=len(steps)
                ):
                    return await self._terminal_policy_violation(
                        steps,
                        sequence_number,
                        decision.tool_name,
                        decision.arguments,
                        decision.rationale,
                        decision.usage,
                        "invalid_evidence_reference",
                        append,
                    )
                step = DiagnosisStep(
                    sequence_number=sequence_number,
                    tool_name=decision.tool_name,
                    arguments=decision.arguments,
                    rationale=decision.rationale,
                    result={"status": "escalated"},
                    usage=decision.usage,
                )
                await append(step)
                return DiagnosisOutcome("escalated", decision.arguments, steps)

            if decision.tool_name not in executable_names:
                return await self._terminal_policy_violation(
                    steps,
                    sequence_number,
                    decision.tool_name,
                    decision.arguments,
                    decision.rationale,
                    decision.usage,
                    "tool_not_allowed",
                    append,
                )

            result = await self.toolbox.execute(
                decision.tool_name, decision.arguments, steps
            )
            step = DiagnosisStep(
                sequence_number=sequence_number,
                tool_name=decision.tool_name,
                arguments=decision.arguments,
                rationale=decision.rationale,
                result=result,
                usage=decision.usage,
            )
            await append(step)

        return DiagnosisOutcome(
            status="escalated",
            report={
                "reason": "step_budget_exhausted",
                "required_action": "Review the audit trail and continue manually.",
                "evidence_step_numbers": [step.sequence_number for step in steps],
            },
            steps=steps,
        )

    async def _terminal_policy_violation(
        self,
        steps: list[DiagnosisStep],
        sequence_number: int,
        requested_tool: str,
        arguments: dict,
        rationale: str,
        usage: dict,
        reason: str,
        append: Callable[[DiagnosisStep], Awaitable[None]],
        *,
        violations: list[dict] | None = None,
        evidence_step_numbers: list[int] | None = None,
    ) -> DiagnosisOutcome:
        result = {"error_code": reason, "requested_tool": requested_tool}
        if violations:
            result["violations"] = violations
        step = DiagnosisStep(
            sequence_number=sequence_number,
            tool_name=requested_tool,
            arguments=arguments,
            rationale=rationale,
            result=result,
            usage=usage,
        )
        await append(step)
        report = {
            "reason": reason,
            "required_action": "Human review is required.",
            "evidence_step_numbers": [*(evidence_step_numbers or []), sequence_number],
        }
        if violations:
            # The rejected conclusion is kept for the reviewer, clearly labelled.
            report["violations"] = violations
            report["rejected_conclusion"] = arguments
        return DiagnosisOutcome("escalated", report, steps)


def _valid_evidence_references(arguments: dict, *, completed_step_count: int) -> bool:
    references = arguments.get("evidence_step_numbers")
    return isinstance(references, list) and bool(references) and all(
        isinstance(value, int)
        and not isinstance(value, bool)
        and 1 <= value <= completed_step_count
        for value in references
    )
