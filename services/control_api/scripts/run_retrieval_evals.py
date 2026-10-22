"""Evaluate manual retrieval against the versioned retrieval dataset.

Runs inside one database transaction that is always rolled back, so it leaves
no rows behind. The database needs the pgvector extension and current
migrations (``alembic upgrade head``).

    # Deterministic, network-free baseline (also what CI can run):
    python scripts/run_retrieval_evals.py

    # The embedding model configured for production (DEVICE_DOCUMENT_EMBEDDING_*):
    python scripts/run_retrieval_evals.py --embedding configured
"""

import argparse
import asyncio
from pathlib import Path

from app.core.config import Settings
from app.db.session import async_session_factory
from app.modules.document.openai_compatible import OpenAICompatibleEmbeddingProvider
from app.modules.document.retrieval_evaluation import (
    DEFAULT_K,
    HashedNgramEmbeddingProvider,
    format_report,
    load_retrieval_dataset,
    run_retrieval_evaluation,
)


async def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset", type=Path, default=Path("evals/retrieval/cases.json")
    )
    parser.add_argument(
        "--embedding",
        choices=["offline", "configured"],
        default="offline",
        help="offline: built-in hashed n-gram baseline; "
        "configured: DEVICE_DOCUMENT_EMBEDDING_* settings",
    )
    parser.add_argument("--k", type=int, default=DEFAULT_K)
    parser.add_argument(
        "--json", type=Path, help="also write the full report (per-case ranks) here"
    )
    parser.add_argument(
        "--no-retry-scenario",
        action="store_true",
        help="skip re-ingesting every manual as a retried extraction run",
    )
    args = parser.parse_args()

    dataset = load_retrieval_dataset(args.dataset)
    settings = Settings()
    if args.embedding == "configured":
        if not settings.document_embedding_model:
            raise SystemExit("DEVICE_DOCUMENT_EMBEDDING_MODEL is required")
        provider = OpenAICompatibleEmbeddingProvider(
            base_url=settings.document_embedding_base_url,
            api_key=settings.document_embedding_api_key,
            model=settings.document_embedding_model,
            timeout_seconds=settings.document_embedding_timeout_seconds,
        )
        embedding_model = settings.document_embedding_model
    else:
        provider = HashedNgramEmbeddingProvider()
        embedding_model = provider.model

    async with async_session_factory() as session:
        try:
            report = await run_retrieval_evaluation(
                session,
                dataset,
                base_dir=args.dataset.parent,
                provider=provider,
                embedding_model=embedding_model,
                k=args.k,
                min_text_chars=settings.document_min_text_chars_per_page,
                include_retry_scenario=not args.no_retry_scenario,
            )
        finally:
            await session.rollback()

    print(format_report(report))
    if args.json:
        args.json.write_text(
            report.model_dump_json(indent=2), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
