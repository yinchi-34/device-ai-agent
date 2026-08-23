from copy import deepcopy
from typing import Any


def _sourced_value(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    sourced = value.get("value")
    return str(sourced).strip().casefold() if sourced is not None else ""


def _scoped_ref(kind: str, group_index: int, original_ref: object) -> str:
    value = str(original_ref).strip() or kind
    return f"{kind}:g{group_index}:{value}"


def merge_candidate_fragments(
    fragments: list[dict[str, Any]],
) -> dict[str, Any]:
    merged: dict[str, Any] = {
        "schema_version": "1.0",
        "device_model": None,
        "profile": None,
        "protocols": [],
        "points": [],
        "capability_matches": [],
        "point_bindings": [],
        "unresolved_suggestions": [],
    }
    for fragment in fragments:
        if not merged["device_model"] and fragment.get("device_model"):
            merged["device_model"] = deepcopy(fragment["device_model"])
        if not merged["profile"] and fragment.get("profile"):
            merged["profile"] = deepcopy(fragment["profile"])

    protocol_by_key: dict[str, dict[str, Any]] = {}
    protocol_aliases: dict[tuple[int, str], str] = {}
    for group_index, fragment in enumerate(fragments, start=1):
        for protocol in fragment.get("protocols", []):
            original_ref = str(protocol.get("candidate_ref", ""))
            key = (
                _sourced_value(protocol.get("protocol"))
                or _sourced_value(protocol.get("code"))
                or f"group:{group_index}:ref:{original_ref}"
            )
            canonical = protocol_by_key.get(key)
            if canonical is None:
                canonical = deepcopy(protocol)
                canonical["candidate_ref"] = _scoped_ref(
                    "protocol", group_index, original_ref
                )
                protocol_by_key[key] = canonical
                merged["protocols"].append(canonical)
            protocol_aliases[(group_index, original_ref)] = canonical["candidate_ref"]

    point_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    point_aliases: dict[tuple[int, str], str] = {}
    for group_index, fragment in enumerate(fragments, start=1):
        for point in fragment.get("points", []):
            original_ref = str(point.get("candidate_ref", ""))
            original_protocol_ref = str(point.get("protocol_ref", ""))
            protocol_ref = protocol_aliases.get(
                (group_index, original_protocol_ref),
                _scoped_ref("unresolved-protocol", group_index, original_protocol_ref),
            )
            key = (
                protocol_ref,
                _sourced_value(point.get("code")),
                _sourced_value(point.get("address_text")),
            )
            canonical = point_by_key.get(key)
            if canonical is None:
                canonical = deepcopy(point)
                canonical["candidate_ref"] = _scoped_ref(
                    "point", group_index, original_ref
                )
                canonical["protocol_ref"] = protocol_ref
                point_by_key[key] = canonical
                merged["points"].append(canonical)
            point_aliases[(group_index, original_ref)] = canonical["candidate_ref"]

    capability_by_code: dict[str, dict[str, Any]] = {}
    capability_aliases: dict[tuple[int, str], str] = {}
    for group_index, fragment in enumerate(fragments, start=1):
        for capability in fragment.get("capability_matches", []):
            original_ref = str(capability.get("candidate_ref", ""))
            key = str(capability.get("capability_code", "")).strip().casefold()
            key = key or f"group:{group_index}:ref:{original_ref}"
            canonical = capability_by_code.get(key)
            if canonical is None:
                canonical = deepcopy(capability)
                canonical["candidate_ref"] = _scoped_ref(
                    "capability", group_index, original_ref
                )
                capability_by_code[key] = canonical
                merged["capability_matches"].append(canonical)
            capability_aliases[(group_index, original_ref)] = canonical["candidate_ref"]

    binding_keys: set[tuple[str, str, str, str]] = set()
    for group_index, fragment in enumerate(fragments, start=1):
        for binding in fragment.get("point_bindings", []):
            canonical = deepcopy(binding)
            original_capability_ref = str(binding.get("capability_ref", ""))
            original_point_ref = str(binding.get("point_ref", ""))
            canonical["capability_ref"] = capability_aliases.get(
                (group_index, original_capability_ref),
                _scoped_ref(
                    "unresolved-capability",
                    group_index,
                    original_capability_ref,
                ),
            )
            canonical["point_ref"] = point_aliases.get(
                (group_index, original_point_ref),
                _scoped_ref("unresolved-point", group_index, original_point_ref),
            )
            key = (
                canonical["capability_ref"],
                canonical["point_ref"],
                str(canonical.get("role", "")),
                str(canonical.get("alias", "")),
            )
            if key not in binding_keys:
                binding_keys.add(key)
                merged["point_bindings"].append(canonical)

        merged["unresolved_suggestions"].extend(
            deepcopy(fragment.get("unresolved_suggestions", []))
        )

    return merged
