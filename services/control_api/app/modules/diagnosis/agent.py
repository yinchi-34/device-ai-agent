from collections.abc import Sequence

from jsonschema import Draft202012Validator

from app.modules.diagnosis.contracts import (
    DiagnosisInput,
    DiagnosisModelProvider,
    DiagnosisOutcome,
    DiagnosisStep,
    DiagnosisToolbox,
    StepCallback,
    ToolDefinition,
)

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
        self, provider: DiagnosisModelProvider, toolbox: DiagnosisToolbox
    ) -> None:
        self.provider = provider
        self.toolbox = toolbox

    async def run(
        self,
        diagnosis: DiagnosisInput,
        *,
        existing_steps: Sequence[DiagnosisStep] = (),
        on_step: StepCallback | None = None,
    ) -> DiagnosisOutcome:
        if not 1 <= diagnosis.max_steps <= 20:
            raise ValueError("max_steps must be between 1 and 20")

        steps = list(existing_steps)
        tool_definitions = list(self.toolbox.definitions())
        executable_names = {tool.name for tool in tool_definitions}
        model_tools = [*tool_definitions, _FINISH_TOOL, _ESCALATE_TOOL]

        while len(steps) < diagnosis.max_steps:
            decision = await self.provider.decide(
                diagnosis=diagnosis,
                steps=steps,
                tools=model_tools,
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
                        on_step,
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
                        on_step,
                    )
                step = DiagnosisStep(
                    sequence_number=sequence_number,
                    tool_name=decision.tool_name,
                    arguments=decision.arguments,
                    rationale=decision.rationale,
                    result={"status": "completed"},
                    usage=decision.usage,
                )
                await _append_step(steps, step, on_step)
                return DiagnosisOutcome("completed", decision.arguments, steps)

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
                        on_step,
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
                        on_step,
                    )
                step = DiagnosisStep(
                    sequence_number=sequence_number,
                    tool_name=decision.tool_name,
                    arguments=decision.arguments,
                    rationale=decision.rationale,
                    result={"status": "escalated"},
                    usage=decision.usage,
                )
                await _append_step(steps, step, on_step)
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
                    on_step,
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
            await _append_step(steps, step, on_step)

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
        on_step: StepCallback | None,
    ) -> DiagnosisOutcome:
        result = {"error_code": reason, "requested_tool": requested_tool}
        step = DiagnosisStep(
            sequence_number=sequence_number,
            tool_name=requested_tool,
            arguments=arguments,
            rationale=rationale,
            result=result,
            usage=usage,
        )
        await _append_step(steps, step, on_step)
        return DiagnosisOutcome(
            "escalated",
            {
                "reason": reason,
                "required_action": "Human review is required.",
                "evidence_step_numbers": [sequence_number],
            },
            steps,
        )


def _valid_evidence_references(arguments: dict, *, completed_step_count: int) -> bool:
    references = arguments.get("evidence_step_numbers")
    return isinstance(references, list) and bool(references) and all(
        isinstance(value, int)
        and not isinstance(value, bool)
        and 1 <= value <= completed_step_count
        for value in references
    )


async def _append_step(
    steps: list[DiagnosisStep],
    step: DiagnosisStep,
    on_step: StepCallback | None,
) -> None:
    steps.append(step)
    if on_step is not None:
        await on_step(step)
