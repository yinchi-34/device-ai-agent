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

    async def decide(self, *, diagnosis, steps, tools, memory=None):
        return self.decisions.pop(0)


def _dataset_path() -> Path:
    return Path(__file__).parents[2] / "evals" / "diagnosis" / "cases.json"


def test_versioned_evaluation_dataset_matches_prompt_baseline():
    dataset = load_evaluation_cases(_dataset_path())

    assert dataset.version == "1.0"
    assert len(dataset.cases) >= 4
    assert dataset.prompt_sha256 == diagnosis_prompt_sha256()
    assert {case.expected.classification for case in dataset.cases} >= {
        "transport",
        "protocol",
        "encoding",
    }


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
    assert report.metrics.faithfulness_rate == 1.0
    assert report.metrics.grounding_intervention_rate == 0.0


async def test_evaluation_scores_faithfulness_to_evidence():
    dataset = load_evaluation_cases(_dataset_path())
    case = next(
        item for item in dataset.cases if item.id == "out_of_range_value_cites_manual"
    )
    read = AgentDecision("read_point", {"connection_id": 1, "point_code": "p"})
    decode = AgentDecision("decode_last_read", {"read_step_sequence": 1})
    search = AgentDecision("search_manual", {"query": "inlet pressure scale"})

    def finish(**extra):
        return AgentDecision(
            "finish_diagnosis",
            {
                "classification": "semantic",
                "confidence": 0.8,
                "evidence_step_numbers": [1, 2, 3],
                **extra,
            },
        )

    scripts = {
        # Faithful: quotes the manual verbatim, cites the decoded value.
        1: [
            read,
            decode,
            search,
            finish(
                summary="Decoded 185.0 bar; the manual's scale differs.",
                cited_values=[{"step": 2, "value": 185.0}],
                manual_citations=[
                    {"step": 3, "stable_ref": "manual-p14-b2", "quote": "scale 0.01"}
                ],
            ),
        ],
        # Hallucinated manual quote: caught by the guard and handed to a human.
        2: [
            read,
            decode,
            search,
            finish(
                summary="Manual says scale 0.001.",
                manual_citations=[
                    {"step": 3, "stable_ref": "manual-p14-b2", "quote": "scale 0.001"}
                ],
            ),
        ],
        # Grounded structure, but the summary invents a number.
        3: [read, decode, search, finish(summary="True pressure is 18.5 bar.")],
    }

    report = await run_evaluation(
        [case],
        provider_factory=lambda case, trial: ScriptedProvider(scripts[trial]),
        trials_per_case=3,
    )

    assert [run.faithful for run in report.runs] == [True, False, False]
    assert [run.grounding_violation for run in report.runs] == [False, True, False]
    assert report.runs[2].unverified_numbers == ["18.5"]
    assert report.metrics.faithfulness_rate == 1 / 3
    assert report.metrics.grounding_intervention_rate == 1 / 3
