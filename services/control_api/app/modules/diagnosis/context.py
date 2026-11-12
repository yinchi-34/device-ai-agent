"""Builds the model's view of a run under a token budget.

Three layers, applied by the runtime on every turn (never left to the model):

1. Slim each tool result: long manual excerpts are clipped, bulky fields dropped.
2. Past the budget, older steps collapse to one-line digests; the most recent
   steps stay in full.
3. The structured working memory always rides along, so what compaction drops
   from the transcript survives as ruled-out causes, readings and suspicions.

Compaction only changes the *view*. The audit trail is stored in full and the
grounding checks run against it, so step numbers cited from a digest still
resolve to the original observation.
"""

import json
from collections.abc import Callable, Sequence
from typing import Any

from app.modules.diagnosis.contracts import DiagnosisInput, DiagnosisStep, WorkingMemory

_MAX_TEXT_CHARS = 400
_MAX_LIST_ITEMS = 20


def estimate_tokens(value: Any) -> int:
    """Cheap, conservative estimate (about 3 characters per token)."""
    return len(json.dumps(value, ensure_ascii=False)) // 3 + 1


def slim_result(tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
    if tool_name == "search_manual":
        return {
            **{k: v for k, v in result.items() if k != "untrusted_manual_evidence"},
            "untrusted_manual_evidence": [
                {
                    "stable_ref": chunk.get("stable_ref"),
                    "page_number": chunk.get("page_number"),
                    "similarity": _round(chunk.get("similarity")),
                    "content": _clip(chunk.get("content", "")),
                }
                for chunk in result.get("untrusted_manual_evidence", [])
            ],
        }
    if tool_name == "decode_last_read":
        # raw_words are already in the read step; applied_config is in the profile.
        return {k: v for k, v in result.items() if k not in {"raw_words"}}
    return _slim_value(result)


def digest_step(step: DiagnosisStep) -> str:
    result = step.result
    status = result.get("status")
    if step.tool_name == "read_point":
        target = f"{step.arguments.get('point_code')}@{step.arguments.get('connection_id')}"
        detail = result.get("error_code") or result.get("exception_name") or ""
        return f"read_point {target} -> {status}{' ' + detail if detail else ''}"
    if step.tool_name == "decode_last_read":
        return (
            f"decode {result.get('point_code')} -> {status} "
            f"value={result.get('engineering_value')} "
            f"in_range={result.get('within_expected_range')}"
        )
    if step.tool_name == "search_manual":
        refs = [c.get("stable_ref") for c in result.get("untrusted_manual_evidence", [])]
        return f"search_manual {step.arguments.get('query', '')[:60]!r} -> refs={refs}"
    if step.tool_name == "list_profile_points":
        codes = [p.get("code") for p in result.get("points", [])]
        return f"list_profile_points -> {codes[:_MAX_LIST_ITEMS]}"
    if step.tool_name == "get_device_context":
        return "get_device_context -> device, profile and connections loaded"
    return f"{step.tool_name} -> {status or 'ok'}"


def build_model_view(
    diagnosis: DiagnosisInput,
    steps: Sequence[DiagnosisStep],
    memory: WorkingMemory,
    *,
    token_budget: int = 6000,
    recent_steps: int = 4,
    estimate: Callable[[Any], int] = estimate_tokens,
) -> dict[str, Any]:
    full = [_full_entry(step) for step in steps]
    keep = len(full) if estimate(full) <= token_budget else min(recent_steps, len(full))

    while True:
        split = len(steps) - keep
        view = {
            "scope": {"device_id": diagnosis.device_id},
            "untrusted_symptom": diagnosis.symptom,
            "target_point_code": diagnosis.target_point_code,
            "remaining_steps": diagnosis.max_steps - len(steps),
            "working_memory": memory.state,
            "compacted_steps": [
                {"sequence_number": s.sequence_number, "digest": digest_step(s)}
                for s in steps[:split]
            ],
            "recent_steps": full[split:],
        }
        if keep <= 1 or estimate(view) <= token_budget:
            break
        keep -= 1

    view["context"] = {
        "compacted_step_count": len(view["compacted_steps"]),
        "estimated_tokens": estimate(view),
        "token_budget": token_budget,
    }
    return view


def _full_entry(step: DiagnosisStep) -> dict[str, Any]:
    return {
        "sequence_number": step.sequence_number,
        "tool_name": step.tool_name,
        "arguments": step.arguments,
        "observation": slim_result(step.tool_name, step.result),
    }


def _slim_value(value: Any) -> Any:
    if isinstance(value, str):
        return _clip(value)
    if isinstance(value, list):
        items = [_slim_value(item) for item in value[:_MAX_LIST_ITEMS]]
        if len(value) > _MAX_LIST_ITEMS:
            items.append(f"... {len(value) - _MAX_LIST_ITEMS} more")
        return items
    if isinstance(value, dict):
        return {key: _slim_value(item) for key, item in value.items()}
    return value


def _clip(text: str) -> str:
    text = str(text)
    if len(text) <= _MAX_TEXT_CHARS:
        return text
    return text[:_MAX_TEXT_CHARS] + f"... [clipped {len(text) - _MAX_TEXT_CHARS} chars]"


def _round(value: Any) -> Any:
    return round(value, 3) if isinstance(value, float) else value
