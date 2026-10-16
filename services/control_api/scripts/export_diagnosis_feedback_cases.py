"""Export every diagnosis run labelled with feedback as an evaluation dataset.

Each run contributes one case labelled by its most recent feedback. The result
can be replayed with ``run_diagnosis_evals.py --dataset <output>``.
"""

import argparse
import asyncio
from pathlib import Path

from app.db.session import async_session_factory
from app.modules.diagnosis import repository
from app.modules.diagnosis.evaluation import EvaluationDataset
from app.modules.diagnosis.feedback import build_evaluation_case
from app.modules.diagnosis.openai_compatible import diagnosis_prompt_sha256


async def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("evals/diagnosis/feedback_cases.json"),
    )
    args = parser.parse_args()

    cases = []
    async with async_session_factory() as session:
        for run_id in await repository.list_run_ids_with_feedback(session):
            run = await repository.get_run(session, run_id)
            feedback = await repository.list_feedback(session, run_id)
            cases.append(build_evaluation_case(run, feedback[-1]))
    if not cases:
        raise SystemExit("No diagnosis runs have feedback yet.")

    dataset = EvaluationDataset(
        version="feedback",
        prompt_sha256=diagnosis_prompt_sha256(),
        cases=cases,
    )
    args.output.write_text(
        dataset.model_dump_json(indent=2, exclude_none=False) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {len(cases)} cases to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
