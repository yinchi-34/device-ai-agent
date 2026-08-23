import json
from pathlib import Path

from jsonschema import Draft202012Validator

from app.modules.document.candidate_validation import (
    validate_candidate_domain,
    validate_candidate_evidence,
    validate_candidate_schema,
)


def sourced(value):
    return {
        "value": value,
        "confidence": 0.95,
        "evidence_refs": ["pdf:page:1:block:1"],
    }


def valid_candidate_payload(capability_code="run_status"):
    return {
        "schema_version": "1.0",
        "device_model": {
            "vendor_name": sourced("Acme"),
            "model_code": sourced("M-1"),
            "model_name": sourced("Meter"),
        },
        "profile": {
            "version": sourced("1.0"),
            "profile_name": sourced("Default"),
        },
        "protocols": [
            {
                "candidate_ref": "protocol:modbus",
                "code": sourced("modbus"),
                "protocol": sourced("modbus_tcp"),
                "config_schema": {},
            }
        ],
        "points": [
            {
                "candidate_ref": "point:run",
                "protocol_ref": "protocol:modbus",
                "code": sourced("run"),
                "name": sourced("Run status"),
                "address_text": sourced("40001"),
                "data_type": sourced("bool"),
                "access_mode": sourced("R"),
                "address_spec": {},
            }
        ],
        "capability_matches": [
            {
                "candidate_ref": "capability:run_status",
                "capability_code": capability_code,
                "confidence": 0.9,
                "evidence_refs": ["pdf:page:1:block:1"],
            }
        ],
        "point_bindings": [
            {
                "capability_ref": "capability:run_status",
                "point_ref": "point:run",
                "role": "status",
                "confidence": 0.9,
                "evidence_refs": ["pdf:page:1:block:1"],
            }
        ],
        "unresolved_suggestions": [],
    }


def test_every_inferred_value_requires_evidence():
    payload = valid_candidate_payload()
    del payload["points"][0]["address_text"]["evidence_refs"]

    errors = validate_candidate_schema(payload)

    assert any("evidence_refs" in error["path"] for error in errors)


def test_exported_json_schema_enforces_nested_evidence():
    payload = valid_candidate_payload()
    del payload["points"][0]["address_text"]["evidence_refs"]
    schema_path = (
        Path(__file__).parents[2]
        / "app"
        / "modules"
        / "document"
        / "candidate_schema.json"
    )

    validator = Draft202012Validator(json.loads(schema_path.read_text()))
    errors = list(validator.iter_errors(payload))

    assert any(
        "evidence_refs" in child.message
        for error in errors
        for child in error.context
    )



def test_evidence_must_resolve_within_the_same_run():
    payload = valid_candidate_payload()

    errors = validate_candidate_evidence(payload, {"pdf:page:2:block:1"})

    assert errors[0]["code"] == "unknown_evidence_ref"
    assert errors[0]["ref"] == "pdf:page:1:block:1"


async def test_unknown_capability_blocks_domain_validation(db_session):
    payload = valid_candidate_payload(capability_code="made_up")

    errors = await validate_candidate_domain(
        db_session, payload, {"pdf:page:1:block:1"}
    )

    assert errors == [
        {
            "code": "unresolved_capability",
            "path": "$.capability_matches[0].capability_code",
            "message": "BasicCapability 'made_up' does not exist",
        }
    ]


async def test_invalid_references_and_enums_are_reported(db_session):
    payload = valid_candidate_payload()
    payload["points"][0]["protocol_ref"] = "protocol:missing"
    payload["points"][0]["access_mode"]["value"] = "RX"
    payload["point_bindings"][0]["point_ref"] = "point:missing"

    errors = await validate_candidate_domain(
        db_session, payload, {"pdf:page:1:block:1"}
    )

    assert {error["code"] for error in errors} >= {
        "unknown_protocol_ref",
        "invalid_access_mode",
        "unknown_point_ref",
    }


async def test_modbus_candidate_rejects_data_type_without_registered_codec(db_session):
    payload = valid_candidate_payload()
    payload["points"][0]["data_type"] = sourced("string")

    errors = await validate_candidate_domain(
        db_session, payload, {"pdf:page:1:block:1"}
    )

    assert any(
        error["code"] == "unsupported_data_type_for_protocol"
        and error["path"] == "$.points[0].data_type.value"
        for error in errors
    )
