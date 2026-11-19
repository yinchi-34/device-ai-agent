import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_replay_cli_writes_a_report_without_model_credentials(tmp_path):
    output = tmp_path / "report.json"
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.run_diagnosis_evals",
            "--mode",
            "replay",
            "--trials",
            "1",
            "--output",
            str(output),
            "--input-price-per-million",
            "1",
            "--output-price-per-million",
            "2",
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert process.returncode == 0, process.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["mode"] == "replay"
    assert report["model"] is None
    assert report["metrics"]["total_runs"] == 40
    assert report["cost"]["average_per_run"] is None


def test_replay_cli_returns_failure_when_expected_classification_is_wrong(tmp_path):
    dataset = json.loads(
        (ROOT / "evals/diagnosis/cases.json").read_text(encoding="utf-8")
    )
    dataset["cases"] = dataset["cases"][:1]
    dataset["cases"][0]["expected"]["classification"] = "healthy"
    dataset_path = tmp_path / "bad_expectation.json"
    dataset_path.write_text(json.dumps(dataset), encoding="utf-8")
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.run_diagnosis_evals",
            "--mode",
            "replay",
            "--trials",
            "1",
            "--dataset",
            str(dataset_path),
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert process.returncode == 1
    assert json.loads(process.stdout)["metrics"]["pass_rate"] == 0
