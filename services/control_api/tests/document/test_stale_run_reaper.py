from datetime import UTC, datetime, timedelta

from sqlalchemy import update

from app.db.session import async_session_factory
from app.modules.document.celery_app import celery_app
from app.modules.document.extraction import (
    _release_run_lock,
    _try_acquire_run_lock,
    reap_stale_extraction_runs,
)
from app.modules.document.models import ProfileExtractionRun
from tests.document.test_extraction_pipeline import pipeline_setup

QUEUED_LIMIT = 6 * 60 * 60
RUNNING_LIMIT = 30 * 60


async def _age_run(session, run_id, *, status, queued_hours=0.0, updated_hours=0.0):
    now = datetime.now(UTC)
    await session.execute(
        update(ProfileExtractionRun)
        .where(ProfileExtractionRun.id == run_id)
        .values(
            status=status,
            queued_at=now - timedelta(hours=queued_hours),
            updated_at=now - timedelta(hours=updated_hours),
        )
    )
    await session.commit()


async def _reap():
    async with async_session_factory() as session:
        return await reap_stale_extraction_runs(
            session,
            stale_queued_seconds=QUEUED_LIMIT,
            stale_running_seconds=RUNNING_LIMIT,
        )


async def _reload(run_id):
    async with async_session_factory() as session:
        return await session.get(ProfileExtractionRun, run_id)


async def test_reaps_run_left_queued_past_the_limit(db_session, tmp_path):
    run, _, _ = await pipeline_setup(db_session, tmp_path)
    await _age_run(db_session, run.id, status="queued", queued_hours=7)

    reaped = await _reap()

    assert reaped == [run.id]
    reloaded = await _reload(run.id)
    assert reloaded.status == "failed"
    assert reloaded.error_code == "stale_queued"
    assert reloaded.error_detail == {"previous_status": "queued"}
    assert reloaded.finished_at is not None


async def test_keeps_recently_queued_run(db_session, tmp_path):
    run, _, _ = await pipeline_setup(db_session, tmp_path)
    await _age_run(db_session, run.id, status="queued", queued_hours=1)

    assert await _reap() == []
    assert (await _reload(run.id)).status == "queued"


async def test_reaps_running_run_whose_worker_is_gone(db_session, tmp_path):
    run, _, _ = await pipeline_setup(db_session, tmp_path)
    await _age_run(db_session, run.id, status="parsing", updated_hours=1)

    reaped = await _reap()

    assert reaped == [run.id]
    reloaded = await _reload(run.id)
    assert reloaded.status == "failed"
    assert reloaded.error_code == "stale_running"
    assert reloaded.error_detail == {"previous_status": "parsing"}


async def test_keeps_old_running_run_while_a_worker_holds_its_lock(
    db_session, tmp_path
):
    run, _, _ = await pipeline_setup(db_session, tmp_path)
    await _age_run(db_session, run.id, status="extracting", updated_hours=5)
    lock = await _try_acquire_run_lock(db_session, run.id)
    assert lock is not None

    try:
        reaped = await _reap()
    finally:
        await _release_run_lock(lock, run.id)

    assert reaped == []
    assert (await _reload(run.id)).status == "extracting"


async def test_leaves_finished_and_failed_runs_alone(db_session, tmp_path):
    run, _, _ = await pipeline_setup(db_session, tmp_path)
    await _age_run(
        db_session, run.id, status="awaiting_review", queued_hours=9, updated_hours=9
    )

    assert await _reap() == []
    assert (await _reload(run.id)).status == "awaiting_review"


def test_beat_runs_reaper_every_ten_minutes():
    celery_app.loader.import_default_modules()
    schedule = celery_app.conf.beat_schedule["reap-stale-extraction-runs"]

    assert schedule["task"] == "document.reap_stale_runs"
    assert schedule["schedule"] == 600
    assert "document.reap_stale_runs" in celery_app.tasks
