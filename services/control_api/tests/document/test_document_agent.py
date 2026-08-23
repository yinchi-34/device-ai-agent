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
