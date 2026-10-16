from collections.abc import Sequence
from typing import Any

from jsonschema import Draft202012Validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.device.enums import ProfileStatus
from app.modules.diagnosis import repository
from app.modules.diagnosis.contracts import DiagnosisStep, ToolDefinition
from app.modules.diagnosis.decoding import DecodeError, decode_register_words
from app.modules.diagnosis.gateway import ReadOnlyRegisterGateway, ReadPointRequest
from app.modules.diagnosis.models import DiagnosisRun
from app.modules.document.providers import EmbeddingProvider, ProviderError
from app.modules.document.retrieval import search_document_chunks
from app.modules.testing import repository as testing_repository

DIAGNOSIS_TOOL_DEFINITIONS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        "get_device_context",
        "Get safe device, released-profile, protocol, and redacted connection metadata.",
        {"type": "object", "additionalProperties": False, "properties": {}},
    ),
    ToolDefinition(
        "list_profile_points",
        "List points and published decoding rules from this device's released profile.",
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"point_code": {"type": "string", "maxLength": 128}},
        },
    ),
    ToolDefinition(
        "search_manual",
        "Search untrusted manual evidence scoped to this device model.",
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 1000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            },
        },
    ),
    ToolDefinition(
        "read_point",
        "Read one named point resolved from the released profile. No address or network target is accepted.",
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["connection_id", "point_code"],
            "properties": {
                "connection_id": {"type": "integer", "minimum": 1},
                "point_code": {"type": "string", "minLength": 1, "maxLength": 128},
            },
        },
    ),
    ToolDefinition(
        "decode_last_read",
        "Decode raw words from a prior successful read using only the released point configuration.",
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["read_step_sequence"],
            "properties": {"read_step_sequence": {"type": "integer", "minimum": 1}},
        },
    ),
)
_DEFINITIONS_BY_NAME = {
    definition.name: definition for definition in DIAGNOSIS_TOOL_DEFINITIONS
}


def validate_tool_arguments(
    name: str, arguments: dict[str, Any]
) -> dict[str, Any] | None:
    """Return the invalid-arguments observation, or None when arguments are valid."""
    errors = sorted(
        Draft202012Validator(_DEFINITIONS_BY_NAME[name].parameters).iter_errors(
            arguments
        ),
        key=lambda error: list(error.path),
    )
    if not errors:
        return None
    return {
        "status": "invalid_tool_arguments",
        "errors": [error.message for error in errors],
    }


def find_successful_read_step(
    read_sequence: int, steps: Sequence[DiagnosisStep]
) -> DiagnosisStep | dict[str, Any]:
    """Resolve a decode reference to a prior successful read, or an error observation."""
    read_step = next(
        (step for step in steps if step.sequence_number == read_sequence), None
    )
    if read_step is None or read_step.tool_name != "read_point":
        return {"status": "read_step_not_found"}
    if read_step.result.get("status") != "success":
        return {"status": "read_step_not_successful"}
    return read_step


def decode_point_words(
    words: Any,
    *,
    point_code: str,
    data_type: str,
    address_spec: dict[str, Any],
    scale: Any,
    offset: Any,
    expected_rule: dict[str, Any] | None,
) -> dict[str, Any]:
    try:
        result = decode_register_words(
            words,
            data_type=data_type,
            word_order=address_spec.get("word_order", "big"),
            byte_order=address_spec.get("byte_order", "big"),
            scale=scale,
            offset=offset,
            expected_rule=expected_rule,
        )
    except (DecodeError, KeyError, TypeError) as exc:
        return {"status": "decode_error", "error": str(exc)}
    return {"status": "success", "point_code": point_code, **result}


class RuntimeDiagnosisToolbox:
    def __init__(
        self,
        session: AsyncSession,
        run: DiagnosisRun,
        *,
        read_gateway: ReadOnlyRegisterGateway,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        self.session = session
        self.run = run
        self.read_gateway = read_gateway
        self.embedding_provider = embedding_provider

    def definitions(self) -> Sequence[ToolDefinition]:
        return list(DIAGNOSIS_TOOL_DEFINITIONS)

    async def execute(
        self, name: str, arguments: dict[str, Any], steps: Sequence[DiagnosisStep]
    ) -> dict[str, Any]:
        invalid = validate_tool_arguments(name, arguments)
        if invalid is not None:
            return invalid
        handlers = {
            "get_device_context": self._get_device_context,
            "list_profile_points": self._list_profile_points,
            "search_manual": self._search_manual,
            "read_point": self._read_point,
            "decode_last_read": self._decode_last_read,
        }
        return await handlers[name](arguments, steps)

    async def _instance(self):
        instance = await testing_repository.get_instance(
            self.session, self.run.device_id
        )
        if instance is None:
            raise RuntimeError("diagnosis device disappeared")
        if instance.profile_version.status != ProfileStatus.RELEASED.value:
            raise RuntimeError("diagnosis profile is no longer released")
        return instance

    async def _get_device_context(self, arguments, steps):
        del arguments, steps
        instance = await self._instance()
        profile = instance.profile_version
        return {
            "device": {
                "id": instance.id,
                "instance_code": instance.instance_code,
                "instance_name": instance.instance_name,
                "location": instance.location,
                "status": instance.status,
                "last_seen_at": instance.last_seen_at.isoformat()
                if instance.last_seen_at
                else None,
            },
            "profile": {
                "id": profile.id,
                "device_model_id": profile.device_model_id,
                "version": profile.version,
                "status": profile.status,
            },
            "protocols": [
                {"id": spec.id, "code": spec.code, "protocol": spec.protocol}
                for spec in profile.protocol_specs
                if not spec.is_deleted
            ],
            "connections": [
                {
                    "id": connection.id,
                    "protocol_spec_id": connection.protocol_spec_id,
                    "name": connection.name,
                    "status": connection.status,
                    "config": _redact_connection_config(connection.config),
                }
                for connection in instance.connections
                if not connection.is_deleted
            ],
        }

    async def _list_profile_points(self, arguments, steps):
        del steps
        instance = await self._instance()
        points = await repository.list_profile_points(
            self.session,
            instance.profile_version_id,
            point_code=arguments.get("point_code"),
        )
        return {
            "points": [
                {
                    "code": point.code,
                    "name": point.name,
                    "address_text": point.address_text,
                    "data_type": point.data_type,
                    "access_mode": point.access_mode,
                    "unit": point.unit,
                    "scale": float(point.scale),
                    "offset": float(point.offset),
                    "address_spec": point.address_spec,
                    "expected_rule": point.expected_rule,
                }
                for point in points
            ]
        }

    async def _search_manual(self, arguments, steps):
        del steps
        if self.embedding_provider is None:
            return {
                "status": "tool_unavailable",
                "error_code": "embedding_provider_not_configured",
            }
        instance = await self._instance()
        try:
            results = await search_document_chunks(
                self.session,
                query=arguments["query"],
                source_document_id=None,
                device_model_id=instance.device_model_id,
                provider=self.embedding_provider,
                limit=arguments.get("limit", 5),
            )
        except ProviderError as exc:
            return {"status": "provider_error", "error_code": exc.code}
        return {
            "untrusted_manual_evidence": [
                {
                    "stable_ref": result.stable_ref,
                    "page_number": result.page_number,
                    "content": result.content,
                    "similarity": result.similarity,
                }
                for result in results
            ]
        }

    async def _read_point(self, arguments, steps):
        retry_error = repeated_read_policy_error(arguments, steps)
        if retry_error is not None:
            return retry_error
        instance = await self._instance()
        points = await repository.list_profile_points(
            self.session,
            instance.profile_version_id,
            point_code=arguments["point_code"],
        )
        if not points:
            return {"status": "point_not_found"}
        point = points[0]
        if "R" not in point.access_mode.upper():
            return {"status": "point_not_readable"}
        connection = await repository.get_device_connection(
            self.session, self.run.device_id, arguments["connection_id"]
        )
        if connection is None:
            return {"status": "connection_not_found"}
        if connection.protocol_spec_id != point.protocol_spec_id:
            return {"status": "connection_protocol_mismatch"}
        spec = next(
            spec
            for spec in instance.profile_version.protocol_specs
            if spec.id == point.protocol_spec_id
        )
        gateway_result = await self.read_gateway.read_point(
            ReadPointRequest(
                device_id=instance.id,
                connection_id=connection.id,
                protocol=spec.protocol,
                point_code=point.code,
                address_text=point.address_text,
                address_spec=point.address_spec,
                data_type=point.data_type,
                access_mode=point.access_mode,
                connection_config=connection.config,
                secret_ref=connection.secret_ref,
            )
        )
        return normalize_read_result(gateway_result, point.code, connection.id)

    async def _decode_last_read(self, arguments, steps):
        read_step = find_successful_read_step(arguments["read_step_sequence"], steps)
        if isinstance(read_step, dict):
            return read_step
        point_code = read_step.result.get("point_code")
        instance = await self._instance()
        points = await repository.list_profile_points(
            self.session, instance.profile_version_id, point_code=point_code
        )
        if not points:
            return {"status": "point_not_found"}
        point = points[0]
        return decode_point_words(
            read_step.result.get("raw_words"),
            point_code=point.code,
            data_type=point.data_type,
            address_spec=point.address_spec,
            scale=point.scale,
            offset=point.offset,
            expected_rule=point.expected_rule,
        )


def repeated_read_policy_error(
    arguments: dict[str, Any], steps: Sequence[DiagnosisStep]
) -> dict[str, Any] | None:
    matching = [
        step
        for step in steps
        if step.tool_name == "read_point" and step.arguments == arguments
    ]
    if not matching:
        return None
    last = matching[-1]
    status = last.result.get("status")
    if status == "timeout" and len(matching) < 3:
        return None
    if status == "timeout":
        error_code = "read_retry_limit_reached"
    elif status in {"connection_refused", "modbus_exception", "success"}:
        error_code = "deterministic_read_result"
    else:
        return None
    return {
        "status": "retry_blocked",
        "error_code": error_code,
        "prior_step_sequence": last.sequence_number,
    }


def _redact_connection_config(config: dict[str, Any]) -> dict[str, Any]:
    sensitive_tokens = ("password", "secret", "token", "credential", "key")
    return {
        key: "[REDACTED]"
        if any(token in key.lower() for token in sensitive_tokens)
        else value
        for key, value in config.items()
    }


def normalize_read_result(
    result: dict[str, Any], point_code: str, connection_id: int
) -> dict[str, Any]:
    allowed_statuses = {
        "success",
        "timeout",
        "connection_refused",
        "modbus_exception",
        "gateway_error",
        "tool_unavailable",
    }
    status = result.get("status")
    if status not in allowed_statuses:
        return {"status": "gateway_error", "error_code": "gateway_invalid_status"}
    normalized: dict[str, Any] = {
        "status": status,
        "point_code": point_code,
        "connection_id": connection_id,
    }
    if status == "success":
        words = result.get("raw_words")
        if not isinstance(words, list) or any(
            not isinstance(word, int)
            or isinstance(word, bool)
            or not 0 <= word <= 0xFFFF
            for word in words
        ):
            return {
                "status": "gateway_error",
                "error_code": "gateway_invalid_raw_words",
            }
        normalized["raw_words"] = words
    for key in ("error_code", "exception_code", "exception_name", "latency_ms"):
        value = result.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            normalized[key] = value
    return normalized
