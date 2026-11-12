import json

import httpx

from app.modules.diagnosis.agent import DiagnosisAgent
from app.modules.diagnosis.context import build_model_view, digest_step, slim_result
from app.modules.diagnosis.contracts import (
    AgentDecision,
    DiagnosisInput,
    DiagnosisStep,
    ToolDefinition,
    WorkingMemory,
)
from app.modules.diagnosis.memory import (
    advance_memory,
    build_memory,
    memory_to_json,
    resume_memory,
)
from app.modules.diagnosis.openai_compatible import OpenAICompatibleDiagnosisProvider

_INPUT = DiagnosisInput(device_id=1, symptom="pressure alarm", max_steps=20)


def _step(n, tool, result, arguments=None, rationale=""):
    return DiagnosisStep(n, tool, arguments or {}, rationale, result)


def _read(n, status, point="pressure", connection=1, **extra):
    return _step(
        n,
        "read_point",
        {"status": status, "point_code": point, "connection_id": connection, **extra},
        {"connection_id": connection, "point_code": point},
    )


def _manual(n, content):
    return _step(
        n,
        "search_manual",
        {
            "untrusted_manual_evidence": [
                {
                    "stable_ref": f"manual-{n}",
                    "page_number": n,
                    "content": content,
                    "similarity": 0.812345,
                }
            ]
        },
        {"query": "pressure"},
    )


# --- layer 1: slimming ----------------------------------------------------------


def test_manual_excerpts_are_clipped_but_keep_their_reference():
    slim = slim_result("search_manual", _manual(1, "x" * 5000).result)
    chunk = slim["untrusted_manual_evidence"][0]

    assert chunk["stable_ref"] == "manual-1"
    assert chunk["page_number"] == 1
    assert chunk["similarity"] == 0.812
    assert len(chunk["content"]) < 500
    assert chunk["content"].endswith("[clipped 4600 chars]")


def test_long_lists_are_truncated_with_a_count():
    slim = slim_result("list_profile_points", {"points": [{"code": i} for i in range(30)]})

    assert len(slim["points"]) == 21
    assert slim["points"][-1] == "... 10 more"


# --- layer 2: compaction ------------------------------------------------------


def test_small_runs_are_sent_in_full():
    steps = [_read(1, "timeout"), _read(2, "success", raw_words=[1])]

    view = build_model_view(_INPUT, steps, build_memory(steps), token_budget=6000)

    assert view["compacted_steps"] == []
    assert [s["sequence_number"] for s in view["recent_steps"]] == [1, 2]


def test_over_budget_runs_compact_older_steps_into_digests():
    steps = [_manual(n, f"page {n} " + "y" * 390) for n in range(1, 11)]

    view = build_model_view(
        _INPUT, steps, build_memory(steps), token_budget=1500, recent_steps=3
    )

    compacted = [s["sequence_number"] for s in view["compacted_steps"]]
    recent = [s["sequence_number"] for s in view["recent_steps"]]
    assert compacted + recent == list(range(1, 11))  # every step stays citable
    assert recent[-1] == 10 and 1 <= len(recent) <= 3
    assert view["context"]["estimated_tokens"] <= 1500
    assert view["context"]["compacted_step_count"] == len(compacted)
    assert view["working_memory"]["manual_refs"][0]["stable_ref"] == "manual-1"


def test_compaction_shrinks_the_recent_window_until_it_fits():
    steps = [_manual(n, "z" * 400) for n in range(1, 7)]

    view = build_model_view(
        _INPUT, steps, build_memory(steps), token_budget=900, recent_steps=4
    )

    assert 1 <= len(view["recent_steps"]) < 4


def test_digests_keep_the_decision_relevant_facts():
    assert (
        digest_step(_read(3, "gateway_error", error_code="gateway_unavailable"))
        == "read_point pressure@1 -> gateway_error gateway_unavailable"
    )
    decode = _step(
        4,
        "decode_last_read",
        {
            "status": "success",
            "point_code": "pressure",
            "engineering_value": 185.0,
            "within_expected_range": False,
        },
    )
    assert digest_step(decode) == (
        "decode pressure -> success value=185.0 in_range=False"
    )


# --- layer 3: structured working memory -----------------------------------------


def test_memory_tracks_ruled_out_causes_reads_evidence_and_suspicions():
    steps = [
        _read(1, "modbus_exception", exception_name="illegal_data_address"),
        _read(2, "success", point="flow", raw_words=[1850]),
        _step(
            3,
            "decode_last_read",
            {
                "status": "success",
                "point_code": "flow",
                "engineering_value": 185.0,
                "unit": "bar",
                "within_expected_range": False,
            },
            rationale="Scale looks 10x off; compare with the manual.",
        ),
    ]

    state = build_memory(steps).state

    assert [r["cause"] for r in state["ruled_out"]] == ["transport@connection:1"]
    assert {s["cause"] for s in state["suspicions"]} == {
        "protocol@pressure",
        "encoding_or_semantic@flow",
    }
    flow = state["points_read"][1]
    assert (flow["engineering_value"], flow["within_expected_range"]) == (185.0, False)
    assert [e["step"] for e in state["evidence"]] == [1, 2, 3]
    assert state["model_hypothesis"] == {
        "step": 3,
        "text": "Scale looks 10x off; compare with the manual.",
    }


def test_a_newer_failure_reopens_a_ruled_out_cause():
    state = build_memory(
        [_read(1, "success", raw_words=[1]), _read(2, "timeout")]
    ).state

    assert state["ruled_out"] == []
    assert state["suspicions"] == [
        {"cause": "transport@connection:1", "reason": "read timed out", "step": 2}
    ]


def test_memory_is_a_pure_fold_over_the_trail():
    steps = [_read(1, "timeout"), _read(2, "connection_refused"), _manual(3, "m")]
    incremental = build_memory([])
    for step in steps:
        incremental = advance_memory(incremental, step)

    assert incremental == build_memory(steps)


def test_resume_reuses_a_current_snapshot_and_rebuilds_a_stale_one():
    steps = [_read(1, "timeout"), _read(2, "success", raw_words=[1])]
    snapshot = memory_to_json(build_memory(steps))

    reused, was_reused = resume_memory(snapshot, steps)
    assert was_reused and reused == build_memory(steps)

    stale = memory_to_json(build_memory(steps[:1]))
    rebuilt, was_reused = resume_memory(stale, steps)
    assert not was_reused and rebuilt == build_memory(steps)

    assert resume_memory({}, steps) == (build_memory(steps), False)


# --- integration -------------------------------------------------------------


class RecordingProvider:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.memories: list[WorkingMemory] = []

    async def decide(self, *, diagnosis, steps, tools, memory=None):
        self.memories.append(memory)
        return self.decisions.pop(0)


class FixedToolbox:
    def definitions(self):
        return [ToolDefinition("read_point", "read", {"type": "object"})]

    async def execute(self, name, arguments, steps):
        return {"status": "connection_refused", "point_code": "p", "connection_id": 1}


async def test_agent_threads_memory_to_the_model_and_to_the_persistence_callback():
    provider = RecordingProvider(
        [
            AgentDecision("read_point", {"connection_id": 1, "point_code": "p"}),
            AgentDecision(
                "finish_diagnosis",
                {
                    "classification": "transport",
                    "summary": "Refused.",
                    "confidence": 0.9,
                    "evidence_step_numbers": [1],
                },
            ),
        ]
    )
    persisted: list[int] = []

    async def on_step(step, memory):
        assert memory.as_of_step == step.sequence_number
        persisted.append(memory.as_of_step)

    await DiagnosisAgent(provider, FixedToolbox()).run(_INPUT, on_step=on_step)

    assert [m.as_of_step for m in provider.memories] == [0, 1]
    assert provider.memories[1].state["suspicions"][0]["cause"] == (
        "transport@connection:1"
    )
    assert persisted == [1, 2]


async def test_agent_ignores_a_stale_snapshot_on_resume():
    provider = RecordingProvider([AgentDecision("escalate_to_human", {})])
    existing = [_read(1, "timeout")]
    stale = WorkingMemory(state={"bogus": True}, as_of_step=0)

    await DiagnosisAgent(provider, FixedToolbox()).run(
        _INPUT, existing_steps=existing, initial_memory=stale
    )

    assert provider.memories[0] == build_memory(existing)


async def test_provider_sends_the_compacted_view_not_the_raw_trail():
    captured = {}

    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "escalate_to_human",
                                        "arguments": "{}",
                                    }
                                }
                            ]
                        }
                    }
                ]
            },
        )

    provider = OpenAICompatibleDiagnosisProvider(
        base_url="https://model.test/v1",
        api_key="k",
        model="m",
        transport=httpx.MockTransport(handler),
        context_token_budget=1200,
        context_recent_steps=2,
    )
    steps = [_manual(n, "w" * 3000) for n in range(1, 9)]

    await provider.decide(diagnosis=_INPUT, steps=steps, tools=[])

    view = json.loads(captured["messages"][1]["content"])
    assert view["context"]["compacted_step_count"] >= 6
    assert "w" * 500 not in captured["messages"][1]["content"]
    assert len(view["working_memory"]["manual_refs"]) == 8
