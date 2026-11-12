import pytest

from app.modules.diagnosis.agent import DiagnosisAgent
from app.modules.diagnosis.contracts import (
    AgentDecision,
    DiagnosisInput,
    DiagnosisStep,
    ToolDefinition,
)
from app.modules.diagnosis.grounding import check_grounding


def _step(n, tool, result, arguments=None):
    return DiagnosisStep(
        sequence_number=n,
        tool_name=tool,
        arguments=arguments or {},
        rationale="",
        result=result,
    )


_REFUSED = _step(1, "read_point", {"status": "connection_refused", "point_code": "p"})
_READ_OK = _step(2, "read_point", {"status": "success", "raw_words": [0, 16672]})
_DECODE_OK = _step(
    3,
    "decode_last_read",
    {"status": "success", "engineering_value": 10.0, "within_expected_range": True},
)
_MANUAL = _step(
    4,
    "search_manual",
    {
        "untrusted_manual_evidence": [
            {
                "stable_ref": "manual-p12-b3",
                "page_number": 12,
                "content": "Pressure is a float32 in   little-endian word order. "
                "Valid range 0 to 16 bar.",
            }
        ]
    },
)
_TIMEOUT_BLOCKED = _step(
    5, "read_point", {"status": "retry_blocked", "error_code": "read_retry_limit_reached"}
)
_STEPS = [_REFUSED, _READ_OK, _DECODE_OK, _MANUAL, _TIMEOUT_BLOCKED]


def _finish(**overrides):
    arguments = {
        "classification": "encoding",
        "summary": "Little word order decodes the value as 10.0.",
        "confidence": 0.9,
        "evidence_step_numbers": [2, 3],
    }
    arguments.update(overrides)
    return arguments


def _rules(report):
    return [violation["rule"] for violation in report.violations]


def test_well_grounded_conclusion_passes():
    report = check_grounding(
        _finish(
            cited_values=[{"step": 3, "value": 10.0}],
            manual_citations=[
                {
                    "step": 4,
                    "stable_ref": "manual-p12-b3",
                    "quote": "little-endian word order",
                }
            ],
        ),
        _STEPS,
    )

    assert report.grounded
    assert report.unverified_numbers == []


def test_policy_steps_cannot_be_cited_as_evidence():
    report = check_grounding(_finish(evidence_step_numbers=[2, 3, 5]), _STEPS)

    assert _rules(report) == ["cited_step_is_not_evidence"]


@pytest.mark.parametrize(
    ("classification", "evidence"),
    [
        ("transport", [2, 3]),  # a successful read is not a transport failure
        ("protocol", [1]),  # a refused connection is not a protocol exception
        ("encoding", [1]),  # no successful decode was cited
        ("healthy", [2]),
    ],
)
def test_classification_must_match_what_was_actually_read(classification, evidence):
    report = check_grounding(
        _finish(classification=classification, evidence_step_numbers=evidence), _STEPS
    )

    assert _rules(report) == ["classification_not_supported"]


def test_healthy_is_contradicted_by_a_cited_failed_read():
    report = check_grounding(
        _finish(classification="healthy", evidence_step_numbers=[1, 2, 3]), _STEPS
    )

    assert _rules(report) == ["classification_contradicted"]


def test_transport_backed_by_a_refused_read_passes():
    report = check_grounding(
        _finish(
            classification="transport",
            summary="Connection refused.",
            evidence_step_numbers=[1],
        ),
        _STEPS,
    )

    assert report.grounded


def test_value_never_observed_is_rejected():
    report = check_grounding(
        _finish(cited_values=[{"step": 3, "value": 12.5}]), _STEPS
    )

    assert _rules(report) == ["cited_value_not_observed"]


def test_manual_quote_must_exist_on_the_cited_page():
    report = check_grounding(
        _finish(
            manual_citations=[
                {"step": 4, "stable_ref": "manual-p12-b3", "quote": "Valid range 0 to 25 bar"}
            ]
        ),
        _STEPS,
    )

    assert _rules(report) == ["manual_quote_not_in_source"]


def test_manual_quote_must_come_from_a_retrieved_chunk():
    report = check_grounding(
        _finish(
            manual_citations=[
                {"step": 4, "stable_ref": "manual-p99-b1", "quote": "float32"},
                {"step": 2, "stable_ref": "manual-p12-b3", "quote": "float32"},
            ]
        ),
        _STEPS,
    )

    assert _rules(report) == [
        "manual_citation_ref_not_retrieved",
        "manual_citation_step_invalid",
    ]


@pytest.mark.parametrize(
    ("overrides", "rule"),
    [
        ({"classification": "unknown"}, "inconclusive_classification"),
        ({"confidence": 0.3}, "low_confidence"),
    ],
)
def test_weak_conclusions_go_to_a_human(overrides, rule):
    assert _rules(check_grounding(_finish(**overrides), _STEPS)) == [rule]


def test_summary_numbers_without_evidence_are_reported():
    report = check_grounding(
        _finish(summary="Value 10.0 at step 3 but the real reading is 42.7 bar."),
        _STEPS,
    )

    assert report.grounded
    assert report.unverified_numbers == ["42.7"]


# --- agent integration --------------------------------------------------------


class Scripted:
    def __init__(self, decisions):
        self.decisions = list(decisions)

    async def decide(self, *, diagnosis, steps, tools, memory=None):
        return self.decisions.pop(0)


class FixedToolbox:
    def __init__(self, results):
        self.results = results

    def definitions(self):
        return [ToolDefinition(name, name, {"type": "object"}) for name in self.results]

    async def execute(self, name, arguments, steps):
        return self.results[name]


async def test_hallucinated_conclusion_is_escalated_with_the_reasons_kept():
    provider = Scripted(
        [
            AgentDecision("read_point", {"connection_id": 1, "point_code": "p"}),
            AgentDecision(
                "finish_diagnosis",
                {
                    "classification": "healthy",
                    "summary": "Reads 21.5 bar, within range.",
                    "confidence": 0.95,
                    "evidence_step_numbers": [1],
                    "cited_values": [{"step": 1, "value": 21.5}],
                },
            ),
        ]
    )
    toolbox = FixedToolbox({"read_point": {"status": "timeout"}})

    outcome = await DiagnosisAgent(provider, toolbox).run(
        DiagnosisInput(device_id=1, symptom="pressure", max_steps=4)
    )

    assert outcome.status == "escalated"
    assert outcome.report["reason"] == "evidence_grounding_failed"
    assert outcome.report["evidence_step_numbers"] == [1, 2]
    assert {v["rule"] for v in outcome.report["violations"]} == {
        "classification_not_supported",
        "cited_value_not_observed",
    }
    assert outcome.report["rejected_conclusion"]["classification"] == "healthy"
    assert outcome.steps[-1].result["error_code"] == "evidence_grounding_failed"


async def test_grounded_conclusion_completes_with_a_grounding_record():
    provider = Scripted(
        [
            AgentDecision("read_point", {"connection_id": 1, "point_code": "p"}),
            AgentDecision(
                "finish_diagnosis",
                {
                    "classification": "transport",
                    "summary": "Port 502 refused the connection.",
                    "confidence": 0.9,
                    "evidence_step_numbers": [1],
                },
            ),
        ]
    )
    toolbox = FixedToolbox({"read_point": {"status": "connection_refused"}})

    outcome = await DiagnosisAgent(provider, toolbox).run(
        DiagnosisInput(device_id=1, symptom="offline", max_steps=4)
    )

    assert outcome.status == "completed"
    assert outcome.report["grounding"] == {
        "checked": True,
        "unverified_numbers": ["502"],
    }
