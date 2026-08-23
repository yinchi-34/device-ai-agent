import argparse
import asyncio
import json
from pathlib import Path

from app.core.config import Settings
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
    args = parser.parse_args()

    dataset = load_evaluation_cases(args.dataset)
    if dataset.prompt_sha256 != diagnosis_prompt_sha256():
        raise SystemExit(
            "Prompt baseline changed; review and update the dataset hash explicitly."
        )
    settings = Settings()
    if not settings.diagnosis_llm_model:
        raise SystemExit("DEVICE_DIAGNOSIS_LLM_MODEL is required")
    report = await run_evaluation(
        dataset.cases,
        provider_factory=_provider_factory(settings),
        trials_per_case=args.trials,
    )
    print(json.dumps(report.model_dump(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
