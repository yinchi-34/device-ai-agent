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
from app.modules.document.evaluation import (
    load_document_dataset,
    run_extraction_evaluation,
    run_retrieval_evaluation,
)
from app.modules.document.openai_compatible import (
    OpenAICompatibleEmbeddingProvider,
    OpenAICompatibleExtractionProvider,
)

_SUITES = ("extraction", "retrieval")


async def _run_extraction(args, settings: Settings, dataset) -> tuple[dict, dict]:
    if not settings.document_llm_model:
        raise SystemExit("DEVICE_DOCUMENT_LLM_MODEL is required for extraction")
    provider = OpenAICompatibleExtractionProvider(
        base_url=settings.document_llm_base_url,
        api_key=settings.document_llm_api_key,
        model=settings.document_llm_model,
        timeout_seconds=settings.document_llm_timeout_seconds,
        max_output_tokens=settings.document_llm_max_output_tokens,
    )
    report = await run_extraction_evaluation(
        dataset,
        provider,
        group_max_tokens=settings.document_extraction_group_max_tokens,
        trials=args.trials,
    )
    body = report.model_dump()
    models = {"document_llm": settings.document_llm_model}
    gate_input = {**body["metrics"], "by_language": body["by_language"]}
    return body, {"models": models, "gate_input": gate_input}


async def _run_retrieval(args, settings: Settings, dataset) -> tuple[dict, dict]:
    if not settings.document_embedding_model:
        raise SystemExit("DEVICE_DOCUMENT_EMBEDDING_MODEL is required for retrieval")
    provider = OpenAICompatibleEmbeddingProvider(
        base_url=settings.document_embedding_base_url,
        api_key=settings.document_embedding_api_key,
        model=settings.document_embedding_model,
        timeout_seconds=settings.document_embedding_timeout_seconds,
    )
    report = await run_retrieval_evaluation(dataset, provider, k=args.k)
    body = report.model_dump()
    models = {"document_embedding": settings.document_embedding_model}
    gate_input = {**body["metrics"], "by_tag": body["by_tag"]}
    return body, {"models": models, "gate_input": gate_input}


async def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--suite",
        action="append",
        choices=_SUITES,
        help="Repeat to run several suites; defaults to all.",
    )
    parser.add_argument(
        "--dataset", type=Path, default=Path("evals/document/dataset.json")
    )
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument(
        "--thresholds-dir", type=Path, default=Path("evals/document")
    )
    parser.add_argument("--output-dir", type=Path, default=Path("runs/evals"))
    args = parser.parse_args()

    dataset = load_document_dataset(args.dataset)
    settings = Settings()
    runners = {"extraction": _run_extraction, "retrieval": _run_retrieval}
    exit_code = 0
    for suite in args.suite or _SUITES:
        body, extra = await runners[suite](args, settings, dataset)
        metadata = new_run_metadata(
            suite,
            dataset.version,
            models=extra["models"],
            parameters={"trials": args.trials, "k": args.k},
        )
        failures = threshold_failures(
            extra["gate_input"],
            load_thresholds(args.thresholds_dir / f"{suite}_thresholds.json"),
        )
        path = write_report(args.output_dir, metadata, body, failures)
        print(f"== {suite}")
        print(json.dumps(body["metrics"], ensure_ascii=False, indent=2))
        print(f"Report: {path}")
        for failure in failures:
            print(f"GATE {failure}")
        exit_code = exit_code or (1 if failures else 0)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
