from pathlib import Path

import pytest

from app.modules.diagnosis.evaluation import (
    ReplayProvider,
    load_evaluation_cases,
    run_evaluation,
)

DATASET = Path(__file__).parents[2] / "evals/diagnosis/cases.json"


@pytest.mark.parametrize(
    "case", load_evaluation_cases(DATASET).cases, ids=lambda case: case.id
)
async def test_individual_replay_scenario(case):
    report = await run_evaluation(
        [case], provider_factory=lambda c, _: ReplayProvider(c)
    )
    assert report.runs[0].passed


async def test_provider_failure_is_counted_as_failure_not_dropped():
    from app.modules.diagnosis.contracts import AgentDecision

    class BrokenProvider:
        async def decide(self, **kwargs):
            raise RuntimeError("sensitive provider response")

    case = load_evaluation_cases(DATASET).cases[0]
    report = await run_evaluation(
        [case], provider_factory=lambda c, _: BrokenProvider()
    )
    assert report.metrics.total_runs == 1
    assert report.metrics.pass_rate == 0
    assert report.runs[0].error == "provider_or_replay_error"
    assert "sensitive" not in report.model_dump_json()

    class WrongProvider:
        async def decide(self, *, steps, **kwargs):
            if not steps:
                return AgentDecision(
                    "read_point", {"connection_id": 1, "point_code": "voltage"}
                )
            return AgentDecision(
                "finish_diagnosis",
                {
                    "classification": "healthy",
                    "summary": "wrong",
                    "confidence": 0.99,
                    "evidence_step_numbers": [1],
                },
            )

    wrong = await run_evaluation([case], provider_factory=lambda c, _: WrongProvider())
    assert wrong.metrics.classification_accuracy == 0
    assert wrong.metrics.pass_rate == 0


async def test_full_replay_suite_has_40_distinct_scenarios_and_honest_metrics():
    dataset = load_evaluation_cases(DATASET)
    assert len(dataset.cases) == 40
    assert len({case.id for case in dataset.cases}) == 40
    assert {case.device_family for case in dataset.cases} >= {
        "meter",
        "inverter",
        "sensor",
    }
    report = await run_evaluation(
        dataset.cases, provider_factory=lambda c, _: ReplayProvider(c)
    )
    assert report.metrics.total_runs == 40
    assert report.metrics.pass_rate == 1
    assert report.metrics.safety_case_count == 10
    assert report.metrics.safety_block_rate == 1
    assert report.metrics.first_call_valid_rate < 1
    assert report.metrics.total_tokens == 0
    assert report.metrics.token_usage_available is False


@pytest.mark.parametrize("bad_trials", [0, -1])
async def test_invalid_repeat_count_is_rejected(bad_trials):
    dataset = load_evaluation_cases(DATASET)
    with pytest.raises(ValueError):
        await run_evaluation(
            dataset.cases,
            provider_factory=lambda c, _: ReplayProvider(c),
            trials_per_case=bad_trials,
        )


async def test_wrong_point_arguments_cannot_receive_expected_fixture_and_pass():
    from app.modules.diagnosis.contracts import AgentDecision

    case = load_evaluation_cases(DATASET).cases[0]

    class WrongPointProvider:
        async def decide(self, *, steps, **kwargs):
            if not steps:
                return AgentDecision(
                    "read_point", {"connection_id": 1, "point_code": "wrong_point"}
                )
            return AgentDecision(
                "finish_diagnosis",
                {
                    "classification": "transport",
                    "summary": "unsupported",
                    "confidence": 0.99,
                    "evidence_step_numbers": [1],
                },
            )

    report = await run_evaluation(
        [case], provider_factory=lambda c, _: WrongPointProvider()
    )
    assert report.metrics.pass_rate == 0


async def test_a_safe_model_handoff_counts_without_attempting_the_attack():
    from app.modules.diagnosis.contracts import AgentDecision

    case = next(c for c in load_evaluation_cases(DATASET).cases if c.id == "tool_write")

    class SafeProvider:
        async def decide(self, *, steps, **kwargs):
            if not steps:
                return AgentDecision(
                    "read_point", {"connection_id": 1, "point_code": "status"}
                )
            return AgentDecision(
                "escalate_to_human",
                {
                    "reason": "Write operation is forbidden.",
                    "required_action": "Manual review",
                    "evidence_step_numbers": [1],
                },
            )

    report = await run_evaluation([case], provider_factory=lambda c, _: SafeProvider())
    assert report.metrics.pass_rate == report.metrics.safety_block_rate == 1
