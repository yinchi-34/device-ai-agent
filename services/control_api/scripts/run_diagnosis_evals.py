import argparse
import asyncio
import json
from pathlib import Path

from app.core.config import Settings
from app.core.evaluation import (
    load_thresholds,
    new_run_metadata,
    threshold_failures,
    write_report,
)
from app.modules.diagnosis.evaluation import (
    EvaluationCase,
    load_evaluation_cases,
    run_evaluation,
)
from app.modules.diagnosis.openai_compatible import (
    OpenAICompatibleDiagnosisProvider,
    diagnosis_prompt_sha256,
)


def _provider_factory(settings: Settings):
    def build(case: EvaluationCase, trial_index: int):
        del case, trial_index
        return OpenAICompatibleDiagnosisProvider(
            base_url=settings.diagnosis_llm_base_url,
            api_key=settings.diagnosis_llm_api_key,
            model=settings.diagnosis_llm_model,
            timeout_seconds=settings.diagnosis_llm_timeout_seconds,
        )

    return build


async def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("evals/diagnosis/cases.json"),
    )
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument(
        "--thresholds",
        type=Path,
        default=Path("evals/diagnosis/thresholds.json"),
    )
    parser.add_argument("--output-dir", type=Path, default=Path("runs/evals"))
    parser.add_argument(
        "--case", action="append", default=[], help="Run only these case ids."
    )
    args = parser.parse_args()

    dataset = load_evaluation_cases(args.dataset)
    if dataset.prompt_sha256 != diagnosis_prompt_sha256():
        raise SystemExit(
            "Prompt baseline changed; review and update the dataset hash explicitly."
        )
    cases = [
        case for case in dataset.cases if not args.case or case.id in args.case
    ]
    if not cases:
        raise SystemExit("No cases selected")
    settings = Settings()
    if not settings.diagnosis_llm_model:
        raise SystemExit("DEVICE_DIAGNOSIS_LLM_MODEL is required")
    metadata = new_run_metadata(
        "diagnosis",
        dataset.version,
        models={"diagnosis_llm": settings.diagnosis_llm_model},
        parameters={
            "trials": args.trials,
            "cases": len(cases),
            "prompt_sha256": dataset.prompt_sha256,
        },
    )
    report = await run_evaluation(
        cases,
        provider_factory=_provider_factory(settings),
        trials_per_case=args.trials,
    )
    body = report.model_dump()
    failures = threshold_failures(
        {**body["metrics"], "by_tag": body["by_tag"]},
        load_thresholds(args.thresholds),
    )
    path = write_report(args.output_dir, metadata, body, failures)

    print(json.dumps(body["metrics"], ensure_ascii=False, indent=2))
    for case in report.cases:
        if case.passed_trials < case.trials:
            print(
                f"FAIL {case.case_id}: {case.passed_trials}/{case.trials} "
                f"{', '.join(case.failure_reasons)}"
            )
    print(f"Report: {path}")
    for failure in failures:
        print(f"GATE {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
