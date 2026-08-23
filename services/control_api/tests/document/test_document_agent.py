from copy import deepcopy

from app.modules.document.agent import merge_candidate_fragments


def sourced(value, ref):
    return {
        "value": value,
        "confidence": 0.9,
        "evidence_refs": [ref],
    }


def fragment(*, group, point_code, address):
    ref = f"pdf:page:{group}:block:{group}"
    return {
        "schema_version": "1.0",
        "device_model": (
            {
                "vendor_name": sourced("Siemens", ref),
                "model_code": sourced("PAC3220", ref),
                "model_name": sourced("PAC3220", ref),
            }
            if group == 1
            else None
        ),
        "profile": ({"version": sourced("2025-02", ref)} if group == 1 else None),
        "protocols": [
            {
                "candidate_ref": "protocol:modbus",
                "code": sourced("modbus", ref),
                "protocol": sourced("modbus_tcp", ref),
                "config_schema": {},
            }
        ],
        "points": [
            {
                "candidate_ref": "point:1",
                "protocol_ref": "protocol:modbus",
                "code": sourced(point_code, ref),
                "name": sourced(point_code, ref),
                "address_text": sourced(address, ref),
                "data_type": sourced("float", ref),
                "access_mode": sourced("R", ref),
                "address_spec": {},
                "value_schema": {},
                "expected_rule": {},
            }
        ],
        "capability_matches": [],
        "point_bindings": [],
        "unresolved_suggestions": [],
    }


def test_merge_candidate_fragments_deduplicates_protocols_and_scopes_refs():
    first = fragment(group=1, point_code="voltage_l1_n", address="1")
    second = fragment(group=2, point_code="voltage_l2_n", address="3")

    merged = merge_candidate_fragments([deepcopy(first), deepcopy(second)])

    assert merged["device_model"]["model_code"]["value"] == "PAC3220"
    assert len(merged["protocols"]) == 1
    assert len(merged["points"]) == 2
    assert len({point["candidate_ref"] for point in merged["points"]}) == 2
    protocol_ref = merged["protocols"][0]["candidate_ref"]
    assert {point["protocol_ref"] for point in merged["points"]} == {protocol_ref}
    assert {
        point["address_text"]["evidence_refs"][0] for point in merged["points"]
    } == {"pdf:page:1:block:1", "pdf:page:2:block:2"}
    assert {
        point["data_type"]["value"] for point in merged["points"]
    } == {"float32"}


def test_merge_candidate_fragments_resolves_protocol_from_sibling_fragment():
    protocol_fragment = fragment(group=1, point_code="ignored", address="1")
    protocol_fragment["points"] = []
    point_fragment = fragment(group=2, point_code="voltage_l1_n", address="1")
    point_fragment["protocols"] = []

    merged = merge_candidate_fragments([protocol_fragment, point_fragment])

    assert len(merged["protocols"]) == 1
    assert len(merged["points"]) == 1
    assert merged["points"][0]["protocol_ref"] == merged["protocols"][0][
        "candidate_ref"
    ]


def test_merge_candidate_fragments_keeps_same_type_protocols_with_distinct_codes():
    protocol_fragment = fragment(group=1, point_code="ignored", address="1")
    protocol_fragment["points"] = []
    protocol_fragment["protocols"][0]["candidate_ref"] = "protocol:primary"
    protocol_fragment["protocols"][0]["code"] = sourced(
        "primary_bus", "pdf:page:1:block:1"
    )
    rtu = deepcopy(protocol_fragment["protocols"][0])
    rtu["candidate_ref"] = "protocol:backup"
    rtu["code"] = sourced("backup_bus", "pdf:page:1:block:1")
    protocol_fragment["protocols"].append(rtu)

    merged = merge_candidate_fragments([protocol_fragment])

    assert [item["code"]["value"] for item in merged["protocols"]] == [
        "primary_bus",
        "backup_bus",
    ]


def test_merge_candidate_fragments_does_not_infer_protocol_from_numeric_address():
    protocol_fragment = fragment(group=1, point_code="ignored", address="1")
    protocol_fragment["points"] = []

    point_fragment = fragment(group=2, point_code="50075", address="50075")
    point_fragment["protocols"] = []
    point_fragment["points"][0]["protocol_ref"] = "pdf:page:127:block:284"

    merged = merge_candidate_fragments([protocol_fragment, point_fragment])

    assert len(merged["points"]) == 1
    assert merged["points"][0]["protocol_ref"].startswith(
        "unresolved-protocol:"
    )
    assert any(
        item.get("type") == "unresolved_protocol_ref"
        for item in merged["unresolved_suggestions"]
    )


def test_merge_candidate_fragments_does_not_bind_unknown_http_ref_to_modbus():
    candidate = fragment(group=1, point_code="status", address="1")
    candidate["points"][0]["protocol_ref"] = "protocol:http"

    merged = merge_candidate_fragments([candidate])

    assert merged["points"][0]["protocol_ref"].startswith(
        "unresolved-protocol:"
    )


def test_merge_candidate_fragments_reports_same_code_definition_conflict():
    first = fragment(group=1, point_code="ignored", address="1")
    first["points"] = []
    second = fragment(group=2, point_code="ignored", address="1")
    second["points"] = []
    second["protocols"][0]["protocol"] = sourced(
        "modbus_rtu", "pdf:page:2:block:2"
    )

    merged = merge_candidate_fragments([first, second])

    assert len(merged["protocols"]) == 1
    assert any(
        item.get("type") == "protocol_definition_conflict"
        and item.get("code") == "modbus"
        for item in merged["unresolved_suggestions"]
    )


def test_merge_candidate_fragments_is_idempotent_and_normalizes_values():
    candidate = fragment(group=1, point_code="voltage_l1_n", address="1")
    candidate["points"][0]["data_type"] = sourced(
        "Unsigned short", "pdf:page:1:block:1"
    )
    candidate["protocols"].append(
        {
            "candidate_ref": "protocol:http",
            "code": sourced("http_web_server", "pdf:page:1:block:1"),
            "protocol": sourced("http", "pdf:page:1:block:1"),
            "config_schema": {},
        }
    )

    first = merge_candidate_fragments([candidate])
    second = merge_candidate_fragments([first])

    assert second["protocols"] == first["protocols"]
    assert second["points"] == first["points"]
    assert second["points"][0]["data_type"]["value"] == "uint16"
    assert [item["protocol"]["value"] for item in second["protocols"]] == [
        "modbus_tcp"
    ]
    assert any(
        item.get("type") == "unsupported_protocol_type"
        for item in second["unresolved_suggestions"]
    )
