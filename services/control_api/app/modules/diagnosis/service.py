from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from app.core.errors import DomainError
from app.modules.device.enums import ProfileStatus
from app.modules.diagnosis import repository
from app.modules.diagnosis.agent import DiagnosisAgent
from app.modules.diagnosis.contracts import DiagnosisInput
from app.modules.diagnosis.contracts import DiagnosisStep as AuditStep
from app.modules.diagnosis.gateway import ReadOnlyRegisterGateway
from app.modules.diagnosis.models import DiagnosisRun, DiagnosisStep
from app.modules.diagnosis.tools import RuntimeDiagnosisToolbox
from app.modules.document.providers import EmbeddingProvider
from app.modules.testing import repository as testing_repository

_DIAGNOSIS_RUN_LOCK_NAMESPACE = 0x44494147


async def _try_acquire_run_lock(
    session: AsyncSession, run_id: int
) -> AsyncConnection | None:
    bind = session.bind
    if not isinstance(bind, AsyncEngine):
        raise TypeError("Diagnosis run locking requires an async engine")
    connection = await bind.connect()
    try:
        acquired = await connection.scalar(
            text("SELECT pg_try_advisory_lock(:namespace, :run_id)"),
            {"namespace": _DIAGNOSIS_RUN_LOCK_NAMESPACE, "run_id": run_id},
        )
    except Exception:
        await connection.close()
        raise
    if acquired:
        return connection
    await connection.close()
    return None


async def _release_run_lock(connection: AsyncConnection, run_id: int) -> None:
    try:
        await connection.execute(
            text("SELECT pg_advisory_unlock(:namespace, :run_id)"),
            {"namespace": _DIAGNOSIS_RUN_LOCK_NAMESPACE, "run_id": run_id},
        )
    finally:
        await connection.close()


class DiagnosisService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_run(
        self,
        *,
        device_id: int,
        symptom: str,
        target_point_code: str | None,
        max_steps: int,
        model_provider: str,
        model_name: str,
    ) -> DiagnosisRun:
        instance = await testing_repository.get_instance(self.session, device_id)
        if instance is None:
            raise DomainError("not_found", f"DeviceInstance {device_id} not found", 404)
        if instance.profile_version.status != ProfileStatus.RELEASED.value:
            raise DomainError(
                "diagnosis_profile_not_released",
                "Diagnosis requires the device's released profile",
                409,
            )
        if target_point_code is not None:
            points = await repository.list_profile_points(
                self.session, instance.profile_version_id, point_code=target_point_code
            )
            if not points:
                raise DomainError(
                    "diagnosis_target_point_not_found",
                    "Target point is not part of the device's released profile",
                    422,
                )
        run = DiagnosisRun(
            device_id=device_id,
            symptom=symptom,
            target_point_code=target_point_code,
            max_steps=max_steps,
            model_provider=model_provider,
            model_name=model_name,
            steps=[],
        )
        self.session.add(run)
        await self.session.flush()
        return run

    async def get_run(self, run_id: int) -> DiagnosisRun:
        run = await repository.get_run(self.session, run_id)
        if run is None:
            raise DomainError("not_found", f"DiagnosisRun {run_id} not found", 404)
        return run

    async def execute(
        self,
        run_id: int,
        *,
        provider,
        read_gateway: ReadOnlyRegisterGateway,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> DiagnosisRun:
        lock_connection = await _try_acquire_run_lock(self.session, run_id)
        if lock_connection is None:
            raise DomainError(
                "diagnosis_already_running",
                f"DiagnosisRun {run_id} is already executing",
                409,
            )
        try:
            return await self._execute_locked(
                run_id,
                provider=provider,
                read_gateway=read_gateway,
                embedding_provider=embedding_provider,
            )
        finally:
            await _release_run_lock(lock_connection, run_id)

    async def _execute_locked(
        self,
        run_id: int,
        *,
        provider,
        read_gateway: ReadOnlyRegisterGateway,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> DiagnosisRun:
        run = await self.get_run(run_id)
        if run.status in {"completed", "escalated"}:
            return run
        run.status = "running"
        run.error_code = None
        run.error_detail = {}
        run.started_at = run.started_at or datetime.now(UTC)
        await self.session.commit()

        existing_steps = [
            AuditStep(
                sequence_number=step.sequence_number,
                tool_name=step.tool_name,
                arguments=step.arguments,
                rationale=step.rationale,
                result=step.result,
                usage=step.usage_metadata,
            )
            for step in run.steps
        ]
        toolbox = RuntimeDiagnosisToolbox(
            self.session,
            run,
            read_gateway=read_gateway,
            embedding_provider=embedding_provider,
        )

        async def persist_step(step: AuditStep) -> None:
            self.session.add(
                DiagnosisStep(
                    diagnosis_run_id=run.id,
                    sequence_number=step.sequence_number,
                    tool_name=step.tool_name,
                    arguments=step.arguments,
                    rationale=step.rationale,
                    result=step.result,
                    usage_metadata=step.usage,
                )
            )
            run.current_step_count = step.sequence_number
            await self.session.commit()

        try:
            outcome = await DiagnosisAgent(provider, toolbox).run(
                DiagnosisInput(
                    device_id=run.device_id,
                    symptom=run.symptom,
                    target_point_code=run.target_point_code,
                    max_steps=run.max_steps,
                ),
                existing_steps=existing_steps,
                on_step=persist_step,
            )
        except RuntimeError as exc:
            run.status = "failed"
            run.error_code = "diagnosis_execution_failed"
            run.error_detail = {"type": type(exc).__name__}
            run.finished_at = datetime.now(UTC)
            await self.session.commit()
            return await self.get_run(run.id)

        run.status = outcome.status
        run.final_report = outcome.report
        run.finished_at = datetime.now(UTC)
        run.usage_metadata = _aggregate_usage(outcome.steps)
        await self.session.commit()
        return await self.get_run(run.id)


def _aggregate_usage(steps: list[AuditStep]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for step in steps:
        for key, value in step.usage.items():
            if isinstance(value, int) and not isinstance(value, bool):
                totals[key] = totals.get(key, 0) + value
    return totals
