import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

from app.core.config import Settings
from app.modules.diagnosis.evaluation import (
    EvaluationCase,
    ReplayProvider,
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
    parser.add_argument("--mode", choices=["replay", "live"], default="live")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--input-price-per-million", type=float)
    parser.add_argument("--output-price-per-million", type=float)
    parser.add_argument("--currency", default="USD")
    args = parser.parse_args()
    prices = (args.input_price_per_million, args.output_price_per_million)
    if any(price is not None for price in prices) and (
        any(price is None for price in prices) or any(price < 0 for price in prices)
    ):
        parser.error("provide both nonnegative input/output prices per million tokens")

    dataset = load_evaluation_cases(args.dataset)
    if dataset.prompt_sha256 != diagnosis_prompt_sha256():
        raise SystemExit(
            "Prompt baseline changed; review and update the dataset hash explicitly."
        )
    if args.env_file:
        load_dotenv(args.env_file, override=False)
    settings = Settings()
    if args.mode == "live" and not settings.diagnosis_llm_model:
        raise SystemExit("DEVICE_DIAGNOSIS_LLM_MODEL is required")
    report = await run_evaluation(
        dataset.cases,
        provider_factory=(
            _provider_factory(settings)
            if args.mode == "live"
            else lambda case, _: ReplayProvider(case)
        ),
        trials_per_case=args.trials,
    )
    result = {
        "mode": args.mode,
        "model": settings.diagnosis_llm_model if args.mode == "live" else None,
        "dataset_version": dataset.version,
        "prompt_sha256": dataset.prompt_sha256,
        "created_at": datetime.now(UTC).isoformat(),
        "measurement_scope": "scripted harness regression"
        if args.mode == "replay"
        else "live model with synthetic tool observations; no real devices",
        **report.model_dump(),
    }
    # Do not infer monetary cost from an unknown tariff or missing usage splits.
    split_usage_available = all(
        run.total_tokens == run.prompt_tokens + run.completion_tokens
        for run in report.runs
    )
    cost = None
    if (
        args.mode == "live"
        and report.metrics.token_usage_available
        and split_usage_available
        and all(price is not None for price in prices)
    ):
        cost = (
            sum(
                run.prompt_tokens * prices[0] + run.completion_tokens * prices[1]
                for run in report.runs
            )
            / 1_000_000
            / report.metrics.total_runs
        )
    result["cost"] = {
        "currency": args.currency,
        "average_per_run": cost,
        "input_price_per_million": prices[0],
        "output_price_per_million": prices[1],
        "note": "Estimate excludes caching tiers, reasoning/tool surcharges and failed calls without usage.",
    }
    content = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content + "\n", encoding="utf-8")
    print(content)
    return 0 if report.metrics.pass_rate == 1 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
