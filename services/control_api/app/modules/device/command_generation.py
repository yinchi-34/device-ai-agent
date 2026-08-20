import hashlib
import json
from typing import Any

from app.core.errors import DomainError
from app.modules.device.models import (
    BasicCapability,
    CapabilityCommandTemplate,
    CapabilityPointBinding,
    DeviceCommand,
    DevicePoint,
    DeviceProfileVersion,
)

_VALID_OPS = {"read", "write", "verify"}
_VALID_SELECTS = {"one", "all"}


def validate_command_template(template: CapabilityCommandTemplate) -> dict[str, Any]:
    """Validate the structured operation template; return the normalized spec."""
    spec = template.operation_template or {}
    operations = spec.get("operations")
    if not isinstance(operations, list) or not operations:
        raise DomainError(
            "invalid_command_template",
            f"Template {template.command_code} must define a non-empty operations list",
            status_code=422,
        )
    for index, op in enumerate(operations):
        if not isinstance(op, dict):
            raise DomainError(
                "invalid_command_template",
                f"Operation #{index} must be an object",
                status_code=422,
            )
        if op.get("op") not in _VALID_OPS:
            raise DomainError(
                "invalid_command_template",
                f"Operation #{index} has unsupported op {op.get('op')!r}",
                status_code=422,
            )
        role = op.get("role")
        if not isinstance(role, str) or not role:
            raise DomainError(
                "invalid_command_template",
                f"Operation #{index} must reference a capability role",
                status_code=422,
            )
        if op.get("select", "one") not in _VALID_SELECTS:
            raise DomainError(
                "invalid_command_template",
                f"Operation #{index} has unsupported select {op.get('select')!r}",
                status_code=422,
            )
    return spec


def _resolve_role_bindings(
    op: dict[str, Any], by_role: dict[str, list[CapabilityPointBinding]]
) -> list[CapabilityPointBinding]:
    role = op["role"]
    select_mode = op.get("select", "one")
    candidates = by_role.get(role, [])
    if select_mode == "one":
        if not candidates:
            raise DomainError(
                "missing_capability_role",
                f"No point bound for role {role!r}",
                status_code=409,
            )
        if len(candidates) > 1:
            raise DomainError(
                "ambiguous_capability_role",
                f"Role {role!r} resolves to {len(candidates)} points; expected exactly one",
                status_code=409,
            )
        return candidates
    if not candidates:
        raise DomainError(
            "missing_capability_role",
            f"No point bound for role {role!r}",
            status_code=409,
        )
    return candidates


def _operation_snapshot(
    op: dict[str, Any], bindings: list[CapabilityPointBinding]
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for binding in bindings:
        point: DevicePoint = binding.device_point
        step: dict[str, Any] = {
            "op": op["op"],
            "role": op["role"],
            "device_point_id": point.id,
            "point_code": point.code,
            "address_text": point.address_text,
            "data_type": point.data_type,
            "access_mode": point.access_mode,
            "scale": str(point.scale),
            "offset": str(point.offset),
            "value_schema": point.value_schema,
            "expected_rule": point.expected_rule,
            "binding_id": binding.id,
        }
        if "value" in op:
            step["value"] = op["value"]
        if op.get("expected") is not None:
            step["expected"] = op["expected"]
        steps.append(step)
    return steps


def _source_hash(
    capability: BasicCapability,
    template: CapabilityCommandTemplate,
    operations: list[dict[str, Any]],
) -> str:
    payload = json.dumps(
        {
            "capability": capability.code,
            "template": template.command_code,
            "template_spec": template.operation_template,
            "operations": operations,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def generate_profile_commands(
    profile: DeviceProfileVersion,
) -> list[DeviceCommand]:
    """Deterministically (re)generate commands for every profile capability.

    Pure function over the already-loaded profile graph; callers persist.
    Raises DomainError on missing/ambiguous roles or invalid templates.
    """
    generated: list[DeviceCommand] = []
    capabilities = sorted(
        (pc for pc in profile.profile_capabilities if not pc.is_deleted),
        key=lambda pc: pc.id,
    )
    for profile_capability in capabilities:
        capability = profile_capability.basic_capability
        templates = sorted(
            (t for t in capability.command_templates if not t.is_deleted),
            key=lambda t: t.id,
        )
        bindings = sorted(
            (b for b in profile_capability.bindings if not b.is_deleted),
            key=lambda b: (b.sort_order, b.id),
        )
        by_role: dict[str, list[CapabilityPointBinding]] = {}
        for binding in bindings:
            by_role.setdefault(binding.role, []).append(binding)

        for template in templates:
            validate_command_template(template)
            operations: list[dict[str, Any]] = []
            for op in template.operation_template["operations"]:
                resolved = _resolve_role_bindings(op, by_role)
                operations.extend(_operation_snapshot(op, resolved))
            generated.append(
                DeviceCommand(
                    profile_version_id=profile.id,
                    profile_capability_id=profile_capability.id,
                    source_template_id=template.id,
                    command_code=f"{capability.code}.{template.command_code}",
                    command_name=template.command_name,
                    access_mode=template.access_mode,
                    operations=operations,
                    source_hash=_source_hash(capability, template, operations),
                    generated=True,
                )
            )
    return generated
