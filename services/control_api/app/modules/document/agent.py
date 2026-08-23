import json
import re
from copy import deepcopy
from typing import Any

from app.modules.device.codecs import DATA_TYPE_ALIASES
from app.modules.device.enums import ProtocolType

_SCOPED_REF_RE = re.compile(
    r"^(?:protocol|point|capability|unresolved-protocol|"
    r"unresolved-point|unresolved-capability):g\d+:"
)
_PROTOCOL_ALIASES = {
    "bacnet_ip": ProtocolType.BACNET_IP.value,
    "bacnetip": ProtocolType.BACNET_IP.value,
    "modbus_rtu": ProtocolType.MODBUS_RTU.value,
    "modbus_serial": ProtocolType.MODBUS_RTU.value,
    "modbus_tcp": ProtocolType.MODBUS_TCP.value,
    "modbustcp": ProtocolType.MODBUS_TCP.value,
    "mqtt": ProtocolType.MQTT.value,
    "opc_ua": ProtocolType.OPCUA.value,
    "opcua": ProtocolType.OPCUA.value,
    "simulator": ProtocolType.SIMULATOR.value,
}
_VALID_PROTOCOLS = {item.value for item in ProtocolType}
_MODBUS_ADDRESS_KEYS = {
    "address",
    "function_code",
    "number_of_registers",
    "offset",
    "register_count",
    "registers",
    "table",
}


def _sourced_value(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    sourced = value.get("value")
    return str(sourced).strip().casefold() if sourced is not None else ""


def _normalized_token(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().casefold()).strip("_")


def _normalized_protocol_value(protocol: dict[str, Any]) -> str:
    for sourced in (protocol.get("protocol"), protocol.get("code")):
        raw = _sourced_value(sourced)
        token = _normalized_token(raw)
        normalized = _PROTOCOL_ALIASES.get(token, token)
        if normalized in _VALID_PROTOCOLS:
            return normalized
    return _sourced_value(protocol.get("protocol"))


def _normalize_sourced_value(
    sourced: object, aliases: dict[str, str]
) -> object:
    if not isinstance(sourced, dict):
        return sourced
    normalized = deepcopy(sourced)
    token = _normalized_token(normalized.get("value", ""))
    if token in aliases:
        normalized["value"] = aliases[token]
    return normalized


def _scoped_ref(kind: str, group_index: int, original_ref: object) -> str:
    value = str(original_ref).strip() or kind
    if re.match(rf"^{re.escape(kind)}:g\d+:", value):
        return value
    return f"{kind}:g{group_index}:{value}"


def _unscoped_ref(value: object) -> str:
    unscoped = str(value).strip()
    while match := _SCOPED_REF_RE.match(unscoped):
        unscoped = unscoped[match.end() :]
    return unscoped


def _add_alias(
    aliases: dict[str, set[str]], alias: object, candidate_ref: str
) -> None:
    value = str(alias).strip().casefold()
    if value:
        aliases.setdefault(value, set()).add(candidate_ref)


def _evidence_refs(value: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for field in ("protocol", "code", "display_name"):
        sourced = value.get(field)
        if isinstance(sourced, dict):
            refs.extend(
                str(ref)
                for ref in sourced.get("evidence_refs", [])
                if str(ref) not in refs
            )
    return refs


def _looks_like_modbus_point(point: dict[str, Any]) -> bool:
    address_spec = point.get("address_spec")
    if isinstance(address_spec, dict) and _MODBUS_ADDRESS_KEYS.intersection(
        address_spec
    ):
        return True
    address_text = _sourced_value(point.get("address_text"))
    if address_text and (
        any(word in address_text for word in ("offset", "register"))
        or bool(re.search(r"\d", address_text))
    ):
        return True
    return bool(re.fullmatch(r"\d+", _sourced_value(point.get("code"))))


def _resolve_protocol_refs(
    *,
    group_index: int,
    original_ref: str,
    point: dict[str, Any],
    aliases_by_group: dict[tuple[int, str], str],
    global_aliases: dict[str, set[str]],
    refs_by_value: dict[str, str],
) -> list[str]:
    direct = aliases_by_group.get((group_index, original_ref))
    if direct is not None:
        return [direct]

    normalized_ref = _normalized_token(_unscoped_ref(original_ref))
    for protocol_value, candidate_ref in refs_by_value.items():
        if protocol_value in normalized_ref:
            return [candidate_ref]

    if "modbus" in normalized_ref or _looks_like_modbus_point(point):
        modbus_refs = [
            candidate_ref
            for protocol_value, candidate_ref in refs_by_value.items()
            if protocol_value.startswith("modbus_")
        ]
        if modbus_refs:
            return modbus_refs

    exact_refs = global_aliases.get(original_ref.strip().casefold(), set())
    if not exact_refs:
        exact_refs = global_aliases.get(
            _unscoped_ref(original_ref).casefold(), set()
        )
    if exact_refs:
        return sorted(exact_refs)
    if len(refs_by_value) == 1:
        return list(refs_by_value.values())
    return []


def _deduplicate_suggestions(
    suggestions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for suggestion in suggestions:
        key = json.dumps(suggestion, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            result.append(suggestion)
    return result


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
    suggestions: list[dict[str, Any]] = []
    for fragment in fragments:
        if not merged["device_model"] and fragment.get("device_model"):
            merged["device_model"] = deepcopy(fragment["device_model"])
        if not merged["profile"] and fragment.get("profile"):
            merged["profile"] = deepcopy(fragment["profile"])
        suggestions.extend(deepcopy(fragment.get("unresolved_suggestions", [])))

    protocol_by_key: dict[str, dict[str, Any]] = {}
    protocol_aliases: dict[tuple[int, str], str] = {}
    global_protocol_aliases: dict[str, set[str]] = {}
    protocol_refs_by_value: dict[str, str] = {}
    protocol_values_by_ref: dict[str, str] = {}
    for group_index, fragment in enumerate(fragments, start=1):
        for protocol in fragment.get("protocols", []):
            original_ref = str(protocol.get("candidate_ref", ""))
            protocol_value = _normalized_protocol_value(protocol)
            if protocol_value not in _VALID_PROTOCOLS:
                suggestions.append(
                    {
                        "type": "unsupported_protocol_type",
                        "protocol": _sourced_value(protocol.get("protocol")),
                        "code": _sourced_value(protocol.get("code")),
                        "evidence_refs": _evidence_refs(protocol),
                    }
                )
                continue
            canonical = protocol_by_key.get(protocol_value)
            if canonical is None:
                canonical = deepcopy(protocol)
                canonical["candidate_ref"] = _scoped_ref(
                    "protocol", group_index, original_ref
                )
                canonical["protocol"] = _normalize_sourced_value(
                    canonical.get("protocol"),
                    {**_PROTOCOL_ALIASES, protocol_value: protocol_value},
                )
                protocol_by_key[protocol_value] = canonical
                merged["protocols"].append(canonical)
            candidate_ref = canonical["candidate_ref"]
            protocol_aliases[(group_index, original_ref)] = candidate_ref
            protocol_refs_by_value[protocol_value] = candidate_ref
            protocol_values_by_ref[candidate_ref] = protocol_value
            for alias in (
                original_ref,
                _unscoped_ref(original_ref),
                _sourced_value(protocol.get("code")),
                _sourced_value(protocol.get("protocol")),
                protocol_value,
            ):
                _add_alias(global_protocol_aliases, alias, candidate_ref)

    point_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    point_aliases: dict[tuple[int, str], str] = {}
    for group_index, fragment in enumerate(fragments, start=1):
        for point in fragment.get("points", []):
            original_ref = str(point.get("candidate_ref", ""))
            original_protocol_ref = str(point.get("protocol_ref", ""))
            protocol_refs = _resolve_protocol_refs(
                group_index=group_index,
                original_ref=original_protocol_ref,
                point=point,
                aliases_by_group=protocol_aliases,
                global_aliases=global_protocol_aliases,
                refs_by_value=protocol_refs_by_value,
            )
            if not protocol_refs:
                protocol_refs = [
                    _scoped_ref(
                        "unresolved-protocol",
                        group_index,
                        _unscoped_ref(original_protocol_ref),
                    )
                ]
            base_ref = _scoped_ref("point", group_index, original_ref)
            aliases: list[str] = []
            for protocol_ref in protocol_refs:
                key = (
                    protocol_ref,
                    _sourced_value(point.get("code")),
                    _sourced_value(point.get("address_text")),
                )
                canonical = point_by_key.get(key)
                if canonical is None:
                    canonical = deepcopy(point)
                    candidate_ref = base_ref
                    if len(protocol_refs) > 1:
                        candidate_ref = (
                            f"{base_ref}:{protocol_values_by_ref[protocol_ref]}"
                        )
                    canonical["candidate_ref"] = candidate_ref
                    canonical["protocol_ref"] = protocol_ref
                    canonical["data_type"] = _normalize_sourced_value(
                        canonical.get("data_type"), DATA_TYPE_ALIASES
                    )
                    canonical["access_mode"] = _normalize_sourced_value(
                        canonical.get("access_mode"),
                        {"r": "R", "rw": "RW", "w": "W"},
                    )
                    point_by_key[key] = canonical
                    merged["points"].append(canonical)
                aliases.append(canonical["candidate_ref"])
            point_aliases[(group_index, original_ref)] = aliases[0]

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
            capability_aliases[(group_index, original_ref)] = canonical[
                "candidate_ref"
            ]

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
                    _unscoped_ref(original_capability_ref),
                ),
            )
            canonical["point_ref"] = point_aliases.get(
                (group_index, original_point_ref),
                _scoped_ref(
                    "unresolved-point",
                    group_index,
                    _unscoped_ref(original_point_ref),
                ),
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

    merged["unresolved_suggestions"] = _deduplicate_suggestions(suggestions)
    return merged
