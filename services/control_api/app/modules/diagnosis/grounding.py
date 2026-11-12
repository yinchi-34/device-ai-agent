"""Content-level checks on a terminal diagnosis.

JSON Schema guarantees the *shape* of ``finish_diagnosis``. It cannot tell
whether the conclusion is true to what the agent actually observed. These checks
compare the model's claims against the persisted audit trail -- the full tool
results, not the compacted view the model was shown -- and any failure turns the
run into a human hand-off instead of a confident wrong answer.
"""

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.modules.diagnosis.contracts import DiagnosisStep

# Results that record a policy decision, not an observation of the device.
_NON_EVIDENCE_STATUSES = {"retry_blocked", "invalid_tool_arguments"}
_POLICY_ERROR_CODES = {
    "tool_not_allowed",
    "terminal_arguments_invalid",
    "invalid_evidence_reference",
    "evidence_grounding_failed",
}
_TRANSPORT_FAILURES = {"connection_refused", "timeout"}
_READ_FAILURES = {
    "connection_refused",
    "timeout",
    "modbus_exception",
    "gateway_error",
    "tool_unavailable",
}
_NUMBER = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class GroundingReport:
    violations: list[dict[str, Any]] = field(default_factory=list)
    unverified_numbers: list[str] = field(default_factory=list)

    @property
    def grounded(self) -> bool:
        return not self.violations


def check_grounding(
    arguments: dict[str, Any],
    steps: Sequence[DiagnosisStep],
    *,
    symptom: str = "",
    min_confidence: float = 0.5,
) -> GroundingReport:
    by_number = {step.sequence_number: step for step in steps}
    cited = [by_number[n] for n in arguments.get("evidence_step_numbers", [])]
    violations: list[dict[str, Any]] = []

    for step in cited:
        if not _is_evidence(step):
            violations.append(
                {"rule": "cited_step_is_not_evidence", "step": step.sequence_number}
            )

    classification = arguments.get("classification")
    violations.extend(_classification_violations(classification, cited))

    confidence = arguments.get("confidence")
    if isinstance(confidence, (int, float)) and confidence < min_confidence:
        violations.append({"rule": "low_confidence", "confidence": confidence})

    for claim in arguments.get("cited_values", []):
        step = by_number.get(claim.get("step"))
        if step is None or not _is_evidence(step):
            violations.append({"rule": "cited_value_step_invalid", **claim})
        elif not _value_occurs(claim.get("value"), step.result):
            violations.append({"rule": "cited_value_not_observed", **claim})

    for citation in arguments.get("manual_citations", []):
        violation = _manual_citation_violation(citation, by_number)
        if violation is not None:
            violations.append(violation)

    return GroundingReport(
        violations=violations,
        unverified_numbers=_unverified_numbers(arguments, cited, symptom),
    )


def _is_evidence(step: DiagnosisStep) -> bool:
    result = step.result
    return (
        step.tool_name not in {"finish_diagnosis", "escalate_to_human"}
        and result.get("status") not in _NON_EVIDENCE_STATUSES
        and result.get("error_code") not in _POLICY_ERROR_CODES
    )


def _classification_violations(
    classification: str | None, cited: Sequence[DiagnosisStep]
) -> list[dict[str, Any]]:
    reads = [step.result for step in cited if step.tool_name == "read_point"]
    decodes = [step.result for step in cited if step.tool_name == "decode_last_read"]
    read_statuses = {result.get("status") for result in reads}
    successful_decodes = [d for d in decodes if d.get("status") == "success"]

    def missing(required: str) -> list[dict[str, Any]]:
        return [
            {
                "rule": "classification_not_supported",
                "classification": classification,
                "requires": required,
            }
        ]

    if classification == "unknown":
        return [{"rule": "inconclusive_classification"}]
    if classification == "transport" and not read_statuses & _TRANSPORT_FAILURES:
        return missing("a cited read that timed out or was refused")
    if classification == "protocol" and "modbus_exception" not in read_statuses:
        return missing("a cited read that returned a protocol exception")
    if classification in {"encoding", "semantic"} and not (
        "success" in read_statuses and successful_decodes
    ):
        return missing("a cited successful read and a cited successful decode")
    if classification == "healthy":
        if not successful_decodes:
            return missing("a cited successful decode")
        if read_statuses & _READ_FAILURES or any(
            d.get("within_expected_range") is False for d in successful_decodes
        ):
            return [
                {
                    "rule": "classification_contradicted",
                    "classification": classification,
                }
            ]
    return []


def _manual_citation_violation(
    citation: dict[str, Any], by_number: dict[int, DiagnosisStep]
) -> dict[str, Any] | None:
    step = by_number.get(citation.get("step"))
    if step is None or step.tool_name != "search_manual":
        return {"rule": "manual_citation_step_invalid", **citation}
    chunks = step.result.get("untrusted_manual_evidence", [])
    chunk = next(
        (c for c in chunks if c.get("stable_ref") == citation.get("stable_ref")), None
    )
    if chunk is None:
        return {"rule": "manual_citation_ref_not_retrieved", **citation}
    if _normalize(citation.get("quote", "")) not in _normalize(chunk.get("content", "")):
        return {"rule": "manual_quote_not_in_source", **citation}
    return None


def _normalize(text: str) -> str:
    return _WHITESPACE.sub(" ", str(text)).strip().casefold()


def _value_occurs(value: Any, result: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return any(_numbers_equal(value, observed) for observed in _numbers_in(result))
    needle = _normalize(value)
    return bool(needle) and any(needle in _normalize(s) for s in _strings_in(result))


def _numbers_equal(left: float, right: float) -> bool:
    return math.isclose(float(left), float(right), rel_tol=1e-6, abs_tol=1e-9)


def _numbers_in(value: Any) -> Iterable[float]:
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        yield value
    elif isinstance(value, str):
        for match in _NUMBER.findall(value):
            yield float(match)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _numbers_in(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _numbers_in(item)


def _strings_in(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings_in(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings_in(item)


def _unverified_numbers(
    arguments: dict[str, Any], cited: Sequence[DiagnosisStep], symptom: str
) -> list[str]:
    """Numbers in the summary that no cited observation contains.

    Reported, not enforced: a summary may legitimately contain derived values.
    The evaluation harness tracks this as a faithfulness signal.
    """
    known = [
        *(n for step in cited for n in _numbers_in(step.result)),
        *(n for step in cited for n in _numbers_in(step.arguments)),
        *_numbers_in(symptom),
        *(float(n) for n in arguments.get("evidence_step_numbers", [])),
        *(
            n
            for claim in arguments.get("cited_values", [])
            for n in _numbers_in(claim.get("value"))
        ),
    ]
    unverified: list[str] = []
    for token in _NUMBER.findall(str(arguments.get("summary", ""))):
        if not any(_numbers_equal(float(token), n) for n in known):
            unverified.append(token)
    return list(dict.fromkeys(unverified))
