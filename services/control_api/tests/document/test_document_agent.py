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


def test_merge_candidate_fragments_expands_generic_modbus_point_to_transports():
    protocol_fragment = fragment(group=1, point_code="ignored", address="1")
    protocol_fragment["points"] = []
    rtu = deepcopy(protocol_fragment["protocols"][0])
    rtu["candidate_ref"] = "protocol:modbus_rtu"
    rtu["code"] = sourced("modbus_rtu", "pdf:page:1:block:1")
    rtu["protocol"] = sourced("modbus_rtu", "pdf:page:1:block:1")
    protocol_fragment["protocols"].append(rtu)

    point_fragment = fragment(group=2, point_code="voltage_l1_n", address="1")
    point_fragment["protocols"] = []

    merged = merge_candidate_fragments([protocol_fragment, point_fragment])

    protocol_refs = {item["candidate_ref"] for item in merged["protocols"]}
    assert len(protocol_refs) == 2
    assert len(merged["points"]) == 2
    assert {item["protocol_ref"] for item in merged["points"]} == protocol_refs


def test_merge_candidate_fragments_infers_modbus_from_register_address():
    protocol_fragment = fragment(group=1, point_code="ignored", address="1")
    protocol_fragment["points"] = []
    rtu = deepcopy(protocol_fragment["protocols"][0])
    rtu["candidate_ref"] = "protocol:modbus_rtu"
    rtu["code"] = sourced("modbus_rtu", "pdf:page:1:block:1")
    rtu["protocol"] = sourced("modbus_rtu", "pdf:page:1:block:1")
    protocol_fragment["protocols"].append(rtu)

    point_fragment = fragment(group=2, point_code="50075", address="50075")
    point_fragment["protocols"] = []
    point_fragment["points"][0]["protocol_ref"] = "pdf:page:127:block:284"

    merged = merge_candidate_fragments([protocol_fragment, point_fragment])

    assert len(merged["points"]) == 2
    assert {item["protocol_ref"] for item in merged["points"]} == {
        item["candidate_ref"] for item in merged["protocols"]
    }


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
