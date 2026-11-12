"""Structured working memory for a diagnosis run.

The memory is a deterministic fold over the audit trail: ``state' =
reduce(state, step)``. Deriving it from observations rather than asking the
model to summarise itself means the memory cannot hallucinate, can be rebuilt
from the trail at any time, and a persisted snapshot can be verified by its
``as_of_step`` before it is trusted on resume.
"""

from collections.abc import Sequence
from copy import deepcopy
from typing import Any

from app.modules.diagnosis.contracts import DiagnosisStep, WorkingMemory

MEMORY_VERSION = 1
_MAX_HYPOTHESIS_CHARS = 400


def empty_state() -> dict[str, Any]:
    return {
        "version": MEMORY_VERSION,
        "points_read": [],
        "ruled_out": [],
        "suspicions": [],
        "evidence": [],
        "manual_refs": [],
        "blocked": [],
        "model_hypothesis": None,
    }


def build_memory(steps: Sequence[DiagnosisStep]) -> WorkingMemory:
    state = empty_state()
    for step in steps:
        state = reduce_state(state, step)
    return WorkingMemory(state=state, as_of_step=len(steps))


def advance_memory(memory: WorkingMemory, step: DiagnosisStep) -> WorkingMemory:
    return WorkingMemory(
        state=reduce_state(memory.state, step), as_of_step=step.sequence_number
    )


def memory_to_json(memory: WorkingMemory) -> dict[str, Any]:
    return {"as_of_step": memory.as_of_step, "state": memory.state}


def resume_memory(
    snapshot: dict[str, Any] | None, steps: Sequence[DiagnosisStep]
) -> tuple[WorkingMemory, bool]:
    """Use the persisted snapshot when it is current, else rebuild from the trail.

    Returns the memory and whether the snapshot was reused.
    """
    if (
        snapshot
        and snapshot.get("as_of_step") == len(steps)
        and isinstance(snapshot.get("state"), dict)
        and snapshot["state"].get("version") == MEMORY_VERSION
    ):
        return WorkingMemory(snapshot["state"], snapshot["as_of_step"]), True
    return build_memory(steps), False


def reduce_state(state: dict[str, Any], step: DiagnosisStep) -> dict[str, Any]:
    state = deepcopy(state)
    n = step.sequence_number
    result = step.result
    status = result.get("status")
    if step.rationale:
        state["model_hypothesis"] = {
            "step": n,
            "text": step.rationale[:_MAX_HYPOTHESIS_CHARS],
        }

    if status == "retry_blocked":
        state["blocked"].append(
            {"step": n, "tool": step.tool_name, "reason": result.get("error_code")}
        )
        return state

    if step.tool_name == "read_point":
        _reduce_read(state, n, step.arguments, result)
    elif step.tool_name == "decode_last_read":
        _reduce_decode(state, n, result)
    elif step.tool_name == "search_manual":
        for chunk in result.get("untrusted_manual_evidence", []):
            state["manual_refs"].append(
                {
                    "step": n,
                    "stable_ref": chunk.get("stable_ref"),
                    "page_number": chunk.get("page_number"),
                }
            )
        if result.get("retrieval_mode") == "keyword":
            _evidence(state, n, "manual search degraded to keyword retrieval")
    return state


def _reduce_read(state, n, arguments, result) -> None:
    status = result.get("status")
    point = result.get("point_code") or arguments.get("point_code")
    connection = result.get("connection_id") or arguments.get("connection_id")
    transport = f"transport@connection:{connection}"
    entry = {"step": n, "point_code": point, "connection_id": connection, "status": status}
    for key in ("error_code", "exception_code", "exception_name"):
        if key in result:
            entry[key] = result[key]
    state["points_read"].append(entry)

    if status == "success":
        _rule_out(state, n, transport, f"read of {point} succeeded")
        _evidence(state, n, f"{point} read succeeded")
    elif status == "connection_refused":
        _suspect(state, n, transport, "connection refused")
        _evidence(state, n, f"{point} read refused at transport level")
    elif status == "timeout":
        _suspect(state, n, transport, "read timed out")
        _evidence(state, n, f"{point} read timed out")
    elif status == "modbus_exception":
        _rule_out(state, n, transport, "device answered with a protocol exception")
        _suspect(state, n, f"protocol@{point}", result.get("exception_name") or "exception")
        _evidence(state, n, f"{point} returned a protocol exception")
    elif status in {"gateway_error", "tool_unavailable"}:
        _evidence(
            state,
            n,
            f"{point} could not be observed: {result.get('error_code', status)}",
        )


def _reduce_decode(state, n, result) -> None:
    point = result.get("point_code")
    status = result.get("status")
    if status == "success":
        for read in reversed(state["points_read"]):
            if read["point_code"] == point and read["status"] == "success":
                read["engineering_value"] = result.get("engineering_value")
                read["unit"] = result.get("unit")
                read["within_expected_range"] = result.get("within_expected_range")
                break
        if result.get("within_expected_range") is False:
            _suspect(state, n, f"encoding_or_semantic@{point}", "value out of range")
            _evidence(
                state,
                n,
                f"{point} decoded to {result.get('engineering_value')}, "
                "outside the expected range",
            )
        else:
            _rule_out(state, n, f"encoding_or_semantic@{point}", "value in range")
            _evidence(state, n, f"{point} decoded to {result.get('engineering_value')}")
    elif status == "decode_error":
        _suspect(state, n, f"encoding@{point}", "decode failed")


def _suspect(state, n, cause, reason) -> None:
    # The newest observation wins: a timeout after a good read reopens transport
    # as an intermittent fault rather than staying "ruled out".
    state["ruled_out"] = [r for r in state["ruled_out"] if r["cause"] != cause]
    state["suspicions"] = [s for s in state["suspicions"] if s["cause"] != cause]
    state["suspicions"].append({"cause": cause, "reason": reason, "step": n})


def _rule_out(state, n, cause, reason) -> None:
    state["suspicions"] = [s for s in state["suspicions"] if s["cause"] != cause]
    if not any(item["cause"] == cause for item in state["ruled_out"]):
        state["ruled_out"].append({"cause": cause, "reason": reason, "step": n})


def _evidence(state, n, fact) -> None:
    state["evidence"].append({"step": n, "fact": fact})
