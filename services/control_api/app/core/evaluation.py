"""Shared pieces of the offline evaluation suites: gates, percentiles, reports."""

import json
import math
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, RootModel, model_validator


class MetricThreshold(BaseModel):
    min: float | None = None
    max: float | None = None

    @model_validator(mode="after")
    def _has_bound(self) -> "MetricThreshold":
        if self.min is None and self.max is None:
            raise ValueError("a threshold needs min or max")
        return self


class EvaluationThresholds(RootModel[dict[str, MetricThreshold]]):
    pass


class RunMetadata(BaseModel):
    suite: str
    dataset_version: str
    started_at: str
    git_commit: str | None = None
    models: dict[str, str] = Field(default_factory=dict)
    parameters: dict[str, Any] = Field(default_factory=dict)


def load_thresholds(path: Path) -> dict[str, MetricThreshold]:
    return EvaluationThresholds.model_validate_json(
        path.read_text(encoding="utf-8")
    ).root


def _lookup(metrics: Mapping[str, object], name: str) -> object:
    value: object = metrics
    for part in name.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


def threshold_failures(
    metrics: Mapping[str, object], thresholds: Mapping[str, MetricThreshold]
) -> list[str]:
    """Return one readable line per metric that misses its gate.

    Dotted names such as ``by_tag.points.recall_at_5`` reach nested groups.
    """
    failures: list[str] = []
    for name, threshold in thresholds.items():
        value = _lookup(metrics, name)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            failures.append(f"{name}: missing from report")
            continue
        if threshold.min is not None and value < threshold.min:
            failures.append(f"{name}: {value:.4f} < min {threshold.min}")
        if threshold.max is not None and value > threshold.max:
            failures.append(f"{name}: {value:.4f} > max {threshold.max}")
    return failures


def percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile; 0.0 for an empty sequence."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def current_git_commit() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            check=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() or None


def new_run_metadata(
    suite: str,
    dataset_version: str,
    *,
    models: Mapping[str, str] | None = None,
    parameters: Mapping[str, Any] | None = None,
) -> RunMetadata:
    return RunMetadata(
        suite=suite,
        dataset_version=dataset_version,
        started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        git_commit=current_git_commit(),
        models=dict(models or {}),
        parameters=dict(parameters or {}),
    )


def write_report(
    output_dir: Path,
    metadata: RunMetadata,
    report: Mapping[str, Any],
    gate_failures: Sequence[str],
) -> Path:
    """Write ``<suite>-<timestamp>.json`` and refresh ``<suite>-latest.json``."""
    output_dir.mkdir(parents=True, exist_ok=True)
    body = {
        "metadata": metadata.model_dump(),
        "gate": {"passed": not gate_failures, "failures": list(gate_failures)},
        **report,
    }
    text = json.dumps(body, ensure_ascii=False, indent=2)
    stamp = metadata.started_at.replace(":", "").replace("+0000", "Z")
    path = output_dir / f"{metadata.suite}-{stamp}.json"
    path.write_text(text, encoding="utf-8")
    (output_dir / f"{metadata.suite}-latest.json").write_text(text, encoding="utf-8")
    return path
