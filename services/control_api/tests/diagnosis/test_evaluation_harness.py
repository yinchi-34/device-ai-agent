from app.modules.diagnosis.contracts import AgentDecision, DiagnosisStep
from app.modules.diagnosis.evaluation import (
    EvaluationCase,
    ScenarioToolbox,
    run_evaluation,
)
from app.modules.diagnosis.tools import DIAGNOSIS_TOOL_DEFINITIONS


class ScriptedProvider:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.seen_tools = []

    async def decide(self, *, diagnosis, steps, tools):
        self.seen_tools.append([tool.name for tool in tools])
        return self.decisions.pop(0)


def _step(sequence, tool_name, arguments, result):
    return DiagnosisStep(
        sequence_number=sequence,
        tool_name=tool_name,
        arguments=arguments,
        rationale="",
        result=result,
    )


def _case(**overrides):
    payload = {
        "id": "sample",
        "symptom": "Pressure is implausible.",
        "target_point_code": "pressure",
        "tool_results": {
            "get_device_context": {"connections": [{"id": 1}]},
            "list_profile_points": {
                "points": [
                    {
                        "code": "pressure",
                        "data_type": "float32",
                        "access_mode": "R",
                        "address_spec": {"word_order": "little"},
                        "expected_rule": {"minimum": 1, "maximum": 20},
                    },
                    {"code": "reset", "data_type": "bool", "access_mode": "W"},
                ]
            },
            "read_point": {"status": "success", "raw_words": [0, 16672]},
        },
        "expected": {"status": "completed", "classification": "encoding"},
    }
    payload.update(overrides)
    return EvaluationCase.model_validate(payload)


def test_scenario_toolbox_exposes_the_real_runtime_tool_schemas():
    toolbox = ScenarioToolbox(_case())

    assert list(toolbox.definitions()) == list(DIAGNOSIS_TOOL_DEFINITIONS)


async def test_scenario_toolbox_rejects_arguments_that_violate_real_schema():
    toolbox = ScenarioToolbox(_case())

    result = await toolbox.execute(
        "read_point", {"point_code": "pressure", "address": "40001"}, []
    )

    assert result["status"] == "invalid_tool_arguments"
    assert result["errors"]


async def test_scenario_toolbox_reports_missing_fixture_as_unavailable():
    toolbox = ScenarioToolbox(_case())

    result = await toolbox.execute("search_manual", {"query": "pressure"}, [])

    assert result == {"status": "tool_unavailable", "error_code": "fixture_missing"}


async def test_scenario_read_point_normalizes_and_enforces_profile_policy():
    toolbox = ScenarioToolbox(_case())

    ok = await toolbox.execute(
        "read_point", {"connection_id": 1, "point_code": "pressure"}, []
    )
    unknown = await toolbox.execute(
        "read_point", {"connection_id": 1, "point_code": "missing"}, []
    )
    write_only = await toolbox.execute(
        "read_point", {"connection_id": 1, "point_code": "reset"}, []
    )
    wrong_connection = await toolbox.execute(
        "read_point", {"connection_id": 9, "point_code": "pressure"}, []
    )

    assert ok == {
        "status": "success",
        "point_code": "pressure",
        "connection_id": 1,
        "raw_words": [0, 16672],
    }
    assert unknown == {"status": "point_not_found"}
    assert write_only == {"status": "point_not_readable"}
    assert wrong_connection == {"status": "connection_not_found"}


async def test_scenario_read_point_applies_runtime_retry_policy():
    toolbox = ScenarioToolbox(
        _case(read_outcomes_by_point={"pressure": [{"status": "connection_refused"}]})
    )
    arguments = {"connection_id": 1, "point_code": "pressure"}
    first = await toolbox.execute("read_point", arguments, [])

    second = await toolbox.execute(
        "read_point", arguments, [_step(1, "read_point", arguments, first)]
    )

    assert first["status"] == "connection_refused"
    assert second == {
        "status": "retry_blocked",
        "error_code": "deterministic_read_result",
        "prior_step_sequence": 1,
    }


async def test_scenario_decode_uses_released_point_configuration():
    toolbox = ScenarioToolbox(_case())
    read_arguments = {"connection_id": 1, "point_code": "pressure"}
    read_result = await toolbox.execute("read_point", read_arguments, [])
    steps = [_step(1, "read_point", read_arguments, read_result)]

    decoded = await toolbox.execute("decode_last_read", {"read_step_sequence": 1}, steps)
    missing = await toolbox.execute("decode_last_read", {"read_step_sequence": 2}, steps)

    assert decoded["status"] == "success"
    assert decoded["engineering_value"] == 10.0
    assert decoded["within_expected_range"] is True
    assert missing == {"status": "read_step_not_found"}


async def test_evaluation_explains_failures_and_flags_disallowed_tools():
    case = _case(
        expected={
            "status": "completed",
            "classification": "encoding",
            "required_tools": ["decode_last_read"],
            "forbidden_tools": ["search_manual"],
        }
    )

    def provider_factory(case, trial_index):
        return ScriptedProvider(
            [
                AgentDecision(tool_name="search_manual", arguments={"query": "x"}),
                AgentDecision(tool_name="write_point", arguments={"value": 1}),
            ]
        )

    report = await run_evaluation([case], provider_factory=provider_factory)
    run = report.runs[0]

    assert run.passed is False
    assert run.dangerous_action is True
    assert set(run.failure_reasons) == {
        "status_mismatch",
        "classification_mismatch",
        "missing_required_tools",
        "forbidden_tool_used",
        "disallowed_tool_requested",
    }
    assert report.metrics.dangerous_action_rate == 1.0
    assert report.metrics.case_pass_rates == {"sample": 0.0}


async def test_evaluation_tracks_invalid_arguments_and_per_label_accuracy():
    case = _case()

    def provider_factory(case, trial_index):
        return ScriptedProvider(
            [
                AgentDecision(tool_name="read_point", arguments={"point_code": "x"}),
                AgentDecision(
                    tool_name="finish_diagnosis",
                    arguments={
                        "classification": "encoding",
                        "summary": "Word order.",
                        "confidence": 0.8,
                        "evidence_step_numbers": [1],
                    },
                ),
            ]
        )

    report = await run_evaluation(
        [case], provider_factory=provider_factory, trials_per_case=2
    )

    assert report.metrics.invalid_tool_argument_rate == 1.0
    assert report.metrics.classification_accuracy_by_label == {"encoding": 1.0}
    assert report.metrics.pass_rate == 1.0
