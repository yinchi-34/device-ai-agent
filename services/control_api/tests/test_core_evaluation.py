import json

import pytest
from pydantic import ValidationError

from app.core.evaluation import (
    MetricThreshold,
    new_run_metadata,
    percentile,
    threshold_failures,
    write_report,
)


def test_threshold_failures_reports_each_missed_gate():
    failures = threshold_failures(
        {"recall": 0.8, "errors": 0.0, "by_tag": {"zh": {"recall": 0.95}}},
        {
            "recall": MetricThreshold(min=0.85),
            "errors": MetricThreshold(max=0.0),
            "by_tag.zh.recall": MetricThreshold(min=0.9),
            "missing": MetricThreshold(min=1),
        },
    )

    assert failures == ["recall: 0.8000 < min 0.85", "missing: missing from report"]


def test_threshold_needs_a_bound():
    with pytest.raises(ValidationError):
        MetricThreshold()


def test_percentile_uses_nearest_rank():
    assert percentile([], 0.95) == 0.0
    assert percentile([5.0, 1.0, 3.0], 0.5) == 3.0
    assert percentile([float(value) for value in range(1, 21)], 0.95) == 19.0


def test_write_report_keeps_a_latest_copy(tmp_path):
    metadata = new_run_metadata("retrieval", "1.0", models={"embedding": "m"})

    path = write_report(tmp_path, metadata, {"metrics": {"hit_at_1": 1.0}}, ["x"])

    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["metadata"]["suite"] == "retrieval"
    assert body["gate"] == {"passed": False, "failures": ["x"]}
    assert (tmp_path / "retrieval-latest.json").read_text(encoding="utf-8") == (
        path.read_text(encoding="utf-8")
    )
