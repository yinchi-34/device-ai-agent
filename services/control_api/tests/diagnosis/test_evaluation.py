from pathlib import Path

from app.modules.diagnosis.contracts import AgentDecision
from app.modules.diagnosis.evaluation import (
    FaultInjectionReadGateway,
    load_evaluation_cases,
    run_evaluation,
)
from app.modules.diagnosis.openai_compatible import diagnosis_prompt_sha256


class ScriptedProvider:
    def __init__(self, decisions):
        self.decisions = list(decisions)

    async def decide(self, *, diagnosis, steps, tools):
        return self.decisions.pop(0)


def _dataset_path() -> Path:
    return Path(__file__).parents[2] / "evals" / "diagnosis" / "cases.json"


def test_versioned_evaluation_dataset_matches_prompt_baseline():
    dataset = load_evaluation_cases(_dataset_path())

    assert dataset.version == "1.1"
    assert len(dataset.cases) >= 15
    assert len({case.id for case in dataset.cases}) == len(dataset.cases)
    assert dataset.prompt_sha256 == diagnosis_prompt_sha256()
    assert {case.expected.classification for case in dataset.cases} >= {
        "transport",
        "protocol",
        "encoding",
        "semantic",
        "healthy",
    }
    tags = {tag for case in dataset.cases for tag in case.tags}
    assert tags >= {"safety", "multilingual", "robustness", "escalation"}
    for case in dataset.cases:
        assert case.tags, case.id
        tools = set(case.tool_results)
        assert set(case.expected.required_tools) <= tools, case.id
        assert set(case.expected.evidence_tools) <= tools, case.id


async def test_fault_injection_gateway_replays_outcomes_per_point():
    gateway = FaultInjectionReadGateway(
        {"temperature": [{"status": "timeout"}, {"status": "success", "raw_words": [1]}]}
    )
    request = type("Request", (), {"point_code": "temperature"})()

    assert await gateway.read_point(request) == {"status": "timeout"}
    assert await gateway.read_point(request) == {
        "status": "success",
        "raw_words": [1],
    }
    assert await gateway.read_point(request) == {
        "status": "success",
        "raw_words": [1],
    }


async def test_repeated_evaluation_reports_quality_safety_cost_and_latency():
    dataset = load_evaluation_cases(_dataset_path())
    case = next(
        item for item in dataset.cases if item.expected.classification == "transport"
    )

    def provider_factory(case, trial_index):
        del case, trial_index
        return ScriptedProvider(
            [
                AgentDecision(
                    tool_name="read_point",
                    arguments={"connection_id": 1, "point_code": "status"},
                    usage={"prompt_tokens": 10, "completion_tokens": 2},
                ),
                AgentDecision(
                    tool_name="finish_diagnosis",
                    arguments={
                        "classification": "transport",
                        "summary": "Connection refused.",
                        "confidence": 0.95,
                        "evidence_step_numbers": [1],
                    },
                    usage={"prompt_tokens": 12, "completion_tokens": 3},
                ),
            ]
        )

    report = await run_evaluation(
        [case], provider_factory=provider_factory, trials_per_case=3
    )

    assert report.metrics.total_runs == 3
    assert report.metrics.classification_accuracy == 1.0
    assert report.metrics.pass_rate == 1.0
    assert report.metrics.escalation_rate == 0.0
    assert report.metrics.dangerous_action_rate == 0.0
    assert report.metrics.average_steps == 2.0
    assert report.metrics.total_tokens == 81
    assert report.metrics.average_latency_ms >= 0


def _transport_case():
    dataset = load_evaluation_cases(_dataset_path())
    return next(
        item for item in dataset.cases if item.id == "transport_connection_refused"
    )


def _finish(classification, evidence):
    return AgentDecision(
        tool_name="finish_diagnosis",
        arguments={
            "classification": classification,
            "summary": "Done.",
            "confidence": 0.9,
            "evidence_step_numbers": evidence,
        },
    )


_READ = AgentDecision(
    tool_name="read_point", arguments={"connection_id": 1, "point_code": "status"}
)


async def test_dangerous_tool_request_fails_the_run_and_is_counted():
    case = _transport_case()

    def provider_factory(case, trial_index):
        return ScriptedProvider(
            [AgentDecision(tool_name="write_point", arguments={"value": 1})]
        )

    report = await run_evaluation([case], provider_factory=provider_factory)

    run = report.runs[0]
    assert run.passed is False
    assert run.dangerous_action is True
    assert run.policy_violation == "tool_not_allowed"
    assert "dangerous_action" in run.failure_reasons
    assert report.metrics.dangerous_action_rate == 1.0
    assert report.metrics.policy_violation_rate == 1.0


async def test_provider_error_fails_only_that_run():
    case = _transport_case()

    class BrokenProvider:
        async def decide(self, *, diagnosis, steps, tools):
            raise RuntimeError("diagnosis_model_provider_error")

    def provider_factory(case, trial_index):
        if trial_index == 1:
            return BrokenProvider()
        return ScriptedProvider([_READ, _finish("transport", [1])])

    report = await run_evaluation(
        [case], provider_factory=provider_factory, trials_per_case=2
    )

    assert [run.status for run in report.runs] == ["error", "completed"]
    assert report.metrics.provider_error_rate == 0.5
    assert report.metrics.pass_rate == 0.5
    assert report.metrics.consistent_pass_rate == 0.0
    assert report.cases[0].passed_trials == 1


async def test_report_must_cite_a_step_from_the_evidence_tools():
    case = _transport_case()

    def provider_factory(case, trial_index):
        return ScriptedProvider(
            [
                AgentDecision(tool_name="get_device_context", arguments={}),
                _READ,
                _finish("transport", [1]),
            ]
        )

    report = await run_evaluation([case], provider_factory=provider_factory)

    assert report.runs[0].evidence_grounded is False
    assert report.runs[0].failure_reasons == ["evidence_not_grounded"]
    assert report.metrics.evidence_grounding_rate == 0.0


async def test_repeating_a_deterministic_read_is_redundant():
    case = _transport_case()

    def provider_factory(case, trial_index):
        return ScriptedProvider([_READ, _READ, _finish("transport", [1, 2])])

    report = await run_evaluation([case], provider_factory=provider_factory)

    assert report.runs[0].redundant_calls == 1
    assert report.metrics.redundant_call_rate == 1 / 3


async def test_report_groups_by_tag_and_builds_confusion():
    dataset = load_evaluation_cases(_dataset_path())
    cases = [
        item
        for item in dataset.cases
        if item.id in {"transport_connection_refused", "protocol_illegal_address"}
    ]

    def provider_factory(case, trial_index):
        return ScriptedProvider([_READ, _finish("transport", [1])])

    report = await run_evaluation(cases, provider_factory=provider_factory)

    assert report.by_tag["transport"].pass_rate == 1.0
    assert report.by_tag["protocol"].pass_rate == 0.0
    assert report.confusion == {
        "protocol": {"transport": 1},
        "transport": {"transport": 1},
    }
    assert report.metrics.classification_accuracy == 0.5
