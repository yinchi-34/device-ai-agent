from collections.abc import Iterable
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.device import repository as device_repository
from app.modules.device.enums import AccessMode, PointRole, ProtocolType
from app.modules.document.schemas import ProfileCandidatePayload


def _json_path(parts: Iterable[object]) -> str:
    path = "$"
    for part in parts:
        path += f"[{part}]" if isinstance(part, int) else f".{part}"
    return path
def _schema_error_path(error: Any) -> str:

    parts = list(error.absolute_path)
    if error.validator == "required" and isinstance(error.instance, dict):
        missing = next(
            name for name in error.validator_value if name not in error.instance
        )
        parts.append(missing)
    return _json_path(parts)

def _actionable_schema_errors(error: Any) -> list[Any]:
    required_errors = [
        child
        for item in error.context
        for child in _actionable_schema_errors(item)
        if child.validator == "required"
    ]
    return required_errors or [error]




def validate_candidate_schema(payload: dict[str, Any]) -> list[dict[str, str]]:
    schema = ProfileCandidatePayload.model_json_schema()
    errors = []
    for schema_error in Draft202012Validator(schema).iter_errors(payload):
        errors.extend(
            {"path": _schema_error_path(error), "message": error.message}
            for error in _actionable_schema_errors(schema_error)
        )
    if errors:
        return sorted(errors, key=lambda error: (error["path"], error["message"]))
    try:
        ProfileCandidatePayload.model_validate(payload)
    except ValidationError as exc:
        return [
            {"path": _json_path(error["loc"]), "message": error["msg"]}
            for error in exc.errors()
        ]
    return []


def _walk_evidence(value: object, path: str = "$"):
    if isinstance(value, dict):
        evidence_refs = value.get("evidence_refs")
        if isinstance(evidence_refs, list):
            yield path, evidence_refs
        for key, item in value.items():
            yield from _walk_evidence(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_evidence(item, f"{path}[{index}]")


def validate_candidate_evidence(
    payload: dict[str, Any], known_refs: set[str]
) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    for path, evidence_refs in _walk_evidence(payload):
        for evidence_ref in evidence_refs:
            if evidence_ref not in known_refs:
                errors.append(
                    {
                        "code": "unknown_evidence_ref",
                        "path": f"{path}.evidence_refs",
                        "ref": evidence_ref,
                        "message": f"Evidence ref '{evidence_ref}' is not in this run",
                    }
                )
    return errors


def _duplicate_ref_errors(
    items: list[dict[str, Any]], section: str
) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        candidate_ref = item["candidate_ref"]
        if candidate_ref in seen:
            errors.append(
                {
                    "code": "duplicate_candidate_ref",
                    "path": f"$.{section}[{index}].candidate_ref",
                    "message": f"Candidate ref '{candidate_ref}' is duplicated",
                }
            )
        seen.add(candidate_ref)
    return errors


async def validate_candidate_domain(
    session: AsyncSession, payload: dict[str, Any], known_refs: set[str]
) -> list[dict[str, str]]:
    if validate_candidate_schema(payload):
        return [
            {
                "code": "candidate_schema_invalid",
                "path": "$",
                "message": "Candidate must satisfy schema before domain validation",
            }
        ]
    errors = validate_candidate_evidence(payload, known_refs)
    for section in ("protocols", "points", "capability_matches"):
        errors.extend(_duplicate_ref_errors(payload[section], section))

    protocol_refs = {item["candidate_ref"] for item in payload["protocols"]}
    point_refs = {item["candidate_ref"] for item in payload["points"]}
    capability_refs = {
        item["candidate_ref"] for item in payload["capability_matches"]
    }
    valid_protocols = {item.value for item in ProtocolType}
    valid_access_modes = {item.value for item in AccessMode}
    valid_roles = {item.value for item in PointRole}

    for index, protocol in enumerate(payload["protocols"]):
        value = protocol["protocol"]["value"]
        if value not in valid_protocols:
            errors.append(
                {
                    "code": "invalid_protocol_type",
                    "path": f"$.protocols[{index}].protocol.value",
                    "message": f"Protocol type '{value}' is not supported",
                }
            )
    for index, point in enumerate(payload["points"]):
        if point["protocol_ref"] not in protocol_refs:
            errors.append(
                {
                    "code": "unknown_protocol_ref",
                    "path": f"$.points[{index}].protocol_ref",
                    "message": f"Protocol ref '{point['protocol_ref']}' does not exist",
                }
            )
        access_mode = point["access_mode"]["value"]
        if access_mode not in valid_access_modes:
            errors.append(
                {
                    "code": "invalid_access_mode",
                    "path": f"$.points[{index}].access_mode.value",
                    "message": f"Access mode '{access_mode}' is not supported",
                }
            )
    for index, capability in enumerate(payload["capability_matches"]):
        code = capability["capability_code"]
        if await device_repository.get_capability_by_code(session, code) is None:
            errors.append(
                {
                    "code": "unresolved_capability",
                    "path": f"$.capability_matches[{index}].capability_code",
                    "message": f"BasicCapability '{code}' does not exist",
                }
            )
    for index, binding in enumerate(payload["point_bindings"]):
        if binding["capability_ref"] not in capability_refs:
            errors.append(
                {
                    "code": "unknown_capability_ref",
                    "path": f"$.point_bindings[{index}].capability_ref",
                    "message": (
                        f"Capability ref '{binding['capability_ref']}' does not exist"
                    ),
                }
            )
        if binding["point_ref"] not in point_refs:
            errors.append(
                {
                    "code": "unknown_point_ref",
                    "path": f"$.point_bindings[{index}].point_ref",
                    "message": f"Point ref '{binding['point_ref']}' does not exist",
                }
            )
        if binding["role"] not in valid_roles:
            errors.append(
                {
                    "code": "invalid_point_role",
                    "path": f"$.point_bindings[{index}].role",
                    "message": f"Point role '{binding['role']}' is not supported",
                }
            )
    if payload["unresolved_suggestions"]:
        errors.append(
            {
                "code": "unresolved_suggestions",
                "path": "$.unresolved_suggestions",
                "message": "Unresolved suggestions must be cleared before approval",
            }
        )
    return errors
