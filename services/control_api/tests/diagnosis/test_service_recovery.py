from dataclasses import dataclass

import pytest

from app.core.errors import DomainError
from app.db.session import async_session_factory
from app.modules.diagnosis.contracts import AgentDecision
from app.modules.diagnosis.gateway import UnavailableReadGateway
from app.modules.diagnosis.models import DiagnosisStep
from app.modules.diagnosis.service import (
    DiagnosisService,
    _release_run_lock,
    _try_acquire_run_lock,
)
from tests.api.test_diagnosis_api import _make_released_device


class ProcessCrash(BaseException):
    pass


@dataclass
class CrashAfterContextProvider:
    calls: int = 0

    async def decide(self, *, diagnosis, steps, tools):
        self.calls += 1
        if self.calls == 1:
            return AgentDecision(tool_name="get_device_context", arguments={})
        raise ProcessCrash


class MustNotRunProvider:
    async def decide(self, *, diagnosis, steps, tools):
        raise AssertionError("locked diagnosis must not execute")


async def _create_run(db_session):
    _, device, _ = await _make_released_device(db_session)
    run = await DiagnosisService(db_session).create_run(
        device_id=device.id,
        symptom="intermittent failure",
        target_point_code=None,
        max_steps=4,
        model_provider="fake",
        model_name="fake",
    )
    await db_session.commit()
    return run


async def test_same_run_cannot_execute_while_advisory_lock_is_held(db_session):
    run = await _create_run(db_session)
    lock_connection = await _try_acquire_run_lock(db_session, run.id)
    assert lock_connection is not None

    try:
        async with async_session_factory() as competing_session:
            with pytest.raises(DomainError) as excinfo:
                await DiagnosisService(competing_session).execute(
                    run.id,
                    provider=MustNotRunProvider(),
                    read_gateway=UnavailableReadGateway(),
                )
    finally:
        await _release_run_lock(lock_connection, run.id)

    assert excinfo.value.code == "diagnosis_already_running"


async def test_completed_step_survives_process_crash(db_session):
    run = await _create_run(db_session)

    with pytest.raises(ProcessCrash):
        await DiagnosisService(db_session).execute(
            run.id,
            provider=CrashAfterContextProvider(),
            read_gateway=UnavailableReadGateway(),
        )

    await db_session.close()
    async with async_session_factory() as verification_session:
        persisted = await DiagnosisService(verification_session).get_run(run.id)
        assert persisted.status == "running"
        assert persisted.current_step_count == 1
        assert len(persisted.steps) == 1
        assert isinstance(persisted.steps[0], DiagnosisStep)
        assert persisted.steps[0].tool_name == "get_device_context"
