from app.modules.diagnosis.contracts import DiagnosisStep
from app.modules.diagnosis.tools import repeated_read_policy_error


def _read_step(sequence, status):
    return DiagnosisStep(
        sequence_number=sequence,
        tool_name="read_point",
        arguments={"connection_id": 2, "point_code": "temperature"},
        rationale="",
        result={"status": status},
    )


def test_connection_refused_is_not_blindly_retried_with_identical_arguments():
    error = repeated_read_policy_error(
        {"connection_id": 2, "point_code": "temperature"},
        [_read_step(1, "connection_refused")],
    )

    assert error == {
        "status": "retry_blocked",
        "error_code": "deterministic_read_result",
        "prior_step_sequence": 1,
    }


def test_timeout_allows_two_bounded_retries_then_stops():
    arguments = {"connection_id": 2, "point_code": "temperature"}
    assert repeated_read_policy_error(arguments, [_read_step(1, "timeout")]) is None
    assert (
        repeated_read_policy_error(
            arguments,
            [
                _read_step(1, "timeout"),
                _read_step(2, "timeout"),
                _read_step(3, "timeout"),
            ],
        )["error_code"]
        == "read_retry_limit_reached"
    )
