from copy import deepcopy

import pytest

from app.modules.document.candidate_editing import (
    CandidateFieldEditError,
    apply_candidate_field_changes,
)
from tests.document.test_extraction_pipeline import candidate_payload


def test_candidate_field_changes_update_typed_values_without_mutating_source():
    original = candidate_payload()
    changes = {
        "/profile/version/value": "2.0-reviewed",
        "/protocols/0/config_schema": '{"type":"object"}',
    }

    updated = apply_candidate_field_changes(original, changes)

    assert updated["profile"]["version"]["value"] == "2.0-reviewed"
    assert updated["protocols"][0]["config_schema"] == {"type": "object"}
    assert original == candidate_payload()


def test_candidate_field_changes_reject_non_editable_evidence_path():
    with pytest.raises(CandidateFieldEditError) as excinfo:
        apply_candidate_field_changes(
            candidate_payload(),
            {"/profile/version/evidence_refs": '["invented"]'},
        )

    assert excinfo.value.code == "candidate_field_not_editable"


def test_candidate_field_changes_report_invalid_json_at_field_path():
    payload = deepcopy(candidate_payload())

    with pytest.raises(CandidateFieldEditError) as excinfo:
        apply_candidate_field_changes(
            payload,
            {"/protocols/0/config_schema": "{not-json"},
        )

    assert excinfo.value.code == "candidate_field_value_invalid"
    assert "/protocols/0/config_schema" in str(excinfo.value)
