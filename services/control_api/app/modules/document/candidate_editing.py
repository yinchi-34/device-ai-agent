import json
from copy import deepcopy
from typing import Any

from app.modules.document.candidate_presenter import ReviewField, present_candidate


class CandidateFieldEditError(ValueError):
    def __init__(self, code: str, path: str, message: str):
        self.code = code
        self.path = path
        super().__init__(f"{path}: {message}")


def _editable_fields(payload: dict[str, Any]) -> dict[str, ReviewField]:
    return {
        field.path: field
        for section in present_candidate(payload, payload)
        for item in section.items
        for field in item.fields
        if field.input_kind != "readonly"
    }


def _parse_value(field: ReviewField, raw_value: str) -> Any:
    try:
        if field.input_kind == "json":
            parsed = json.loads(raw_value)
            if not isinstance(parsed, (dict, list)):
                raise ValueError("JSON value must be an object or array")
            return parsed
        if field.input_kind == "integer":
            return None if raw_value.strip() == "" else int(raw_value)
        if field.input_kind == "boolean":
            if raw_value == "true":
                return True
            if raw_value == "false":
                return False
            raise ValueError("boolean value must be true or false")
        return raw_value
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CandidateFieldEditError(
            "candidate_field_value_invalid", field.path, str(exc)
        ) from None


def _set_path(payload: dict[str, Any], path: str, value: Any) -> None:
    segments = path.removeprefix("/").split("/")
    target: Any = payload
    for segment in segments[:-1]:
        target = target[int(segment)] if isinstance(target, list) else target[segment]
    leaf = segments[-1]
    if isinstance(target, list):
        target[int(leaf)] = value
    else:
        target[leaf] = value


def apply_candidate_field_changes(
    payload: dict[str, Any], changes: dict[str, str]
) -> dict[str, Any]:
    editable = _editable_fields(payload)
    updated = deepcopy(payload)
    for path, raw_value in changes.items():
        field = editable.get(path)
        if field is None:
            raise CandidateFieldEditError(
                "candidate_field_not_editable", path, "field is not editable"
            )
        _set_path(updated, path, _parse_value(field, raw_value))
    return updated
