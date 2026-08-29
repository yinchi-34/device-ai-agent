from copy import deepcopy

from app.modules.document.candidate_presenter import present_candidate
from tests.document.test_extraction_pipeline import candidate_payload


def test_candidate_presenter_groups_domain_objects_and_preserves_evidence():
    payload = candidate_payload()

    sections = present_candidate(payload, payload)

    assert [section.key for section in sections] == [
        "device_model",
        "profile",
        "protocols",
        "points",
        "capability_matches",
        "point_bindings",
    ]
    vendor = next(
        field for field in sections[0].items[0].fields if field.key == "vendor_name"
    )
    assert vendor.label == "厂商"
    assert vendor.value == "Acme"
    assert vendor.confidence == 0.95
    assert vendor.evidence_refs == ("docx:block:1",)
    assert sections[2].items[0].title == "modbus"
    assert sections[4].items[0].title == "run_status"


def test_candidate_presenter_marks_human_changes_against_original_payload():
    original = candidate_payload()
    reviewed = deepcopy(original)
    reviewed["profile"]["version"]["value"] = "2.0-reviewed"

    sections = present_candidate(original, reviewed)

    version = sections[1].items[0].fields[0]
    assert version.path == "/profile/version/value"
    assert version.value == "2.0-reviewed"
    assert version.original_value == "1.0"
    assert version.changed is True


def test_candidate_presenter_keeps_empty_sections_visible():
    payload = candidate_payload()

    sections = present_candidate(payload, payload)

    assert sections[3].key == "points"
    assert sections[3].items == ()
    assert sections[3].empty_message == "未提取到点位"
