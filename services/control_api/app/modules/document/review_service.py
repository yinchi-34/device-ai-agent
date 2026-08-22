from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.modules.device import repository as device_repository
from app.modules.device.models import DeviceModel, DeviceProfileVersion
from app.modules.device.service import DeviceService
from app.modules.document import candidate_validation
from app.modules.document.enums import CandidateStatus, ExtractionRunStatus
from app.modules.document.models import (
    DocumentChunk,
    ProfileExtractionCandidate,
    ProfileExtractionRun,
)
from app.modules.document.repository import DocumentRepository


def _sourced_value(value: dict[str, Any] | None) -> Any:
    return value.get("value") if value is not None else None


class DocumentReviewService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.repository = DocumentRepository(session)
        self.device_service = DeviceService(session)

    async def _candidate_for_update(
        self, candidate_id: int
    ) -> ProfileExtractionCandidate:
        candidate = await self.repository.get_candidate_for_update(candidate_id)
        if candidate is None:
            raise DomainError("not_found", f"Candidate {candidate_id} not found", 404)
        return candidate

    async def _known_refs(self, run_id: int) -> set[str]:
        refs = await self.session.scalars(
            select(DocumentChunk.stable_ref).where(
                DocumentChunk.extraction_run_id == run_id
            )
        )
        return set(refs.all())

    async def _validate_locked(
        self, candidate: ProfileExtractionCandidate
    ) -> ProfileExtractionCandidate:
        payload = candidate.reviewed_payload
        schema_errors = candidate_validation.validate_candidate_schema(payload)
        domain_errors = []
        if not schema_errors:
            domain_errors = await candidate_validation.validate_candidate_domain(
                self.session,
                payload,
                await self._known_refs(candidate.extraction_run_id),
            )
        candidate.schema_errors = schema_errors
        candidate.domain_errors = domain_errors
        candidate.status = (
            CandidateStatus.VALID.value
            if not schema_errors and not domain_errors
            else CandidateStatus.INVALID.value
        )
        await self.session.flush()
        return candidate

    async def update_candidate(
        self,
        candidate_id: int,
        *,
        revision: int,
        payload: dict[str, Any],
    ) -> ProfileExtractionCandidate:
        candidate = await self._candidate_for_update(candidate_id)
        if candidate.revision != revision:
            raise DomainError(
                "candidate_revision_conflict",
                "Candidate was modified by another reviewer",
                409,
            )
        if candidate.status == CandidateStatus.IMPORTED.value:
            raise DomainError(
                "candidate_already_imported", "Imported candidates are immutable", 409
            )
        candidate.reviewed_payload = payload
        candidate.revision += 1
        candidate.review_note = None
        candidate.reviewed_at = None
        candidate.status = CandidateStatus.PENDING.value
        return await self._validate_locked(candidate)

    async def validate_candidate(
        self, candidate_id: int
    ) -> ProfileExtractionCandidate:
        return await self._validate_locked(
            await self._candidate_for_update(candidate_id)
        )

    async def approve_candidate(
        self, candidate_id: int, *, revision: int
    ) -> ProfileExtractionCandidate:
        candidate = await self._candidate_for_update(candidate_id)
        if candidate.revision != revision:
            raise DomainError(
                "candidate_revision_conflict",
                "Candidate was modified by another reviewer",
                409,
            )
        if candidate.status != CandidateStatus.VALID.value:
            raise DomainError(
                "candidate_invalid", "Only a valid candidate can be approved", 409
            )
        if candidate.reviewed_payload.get("unresolved_suggestions"):
            raise DomainError(
                "candidate_invalid", "Unresolved suggestions block approval", 409
            )
        candidate.status = CandidateStatus.APPROVED.value
        candidate.reviewed_at = datetime.now(UTC)
        await self.session.flush()
        return candidate

    async def reject_candidate(
        self, candidate_id: int, *, revision: int, note: str
    ) -> ProfileExtractionCandidate:
        candidate = await self._candidate_for_update(candidate_id)
        if candidate.revision != revision:
            raise DomainError(
                "candidate_revision_conflict",
                "Candidate was modified by another reviewer",
                409,
            )
        if candidate.status == CandidateStatus.IMPORTED.value:
            raise DomainError(
                "candidate_already_imported", "Imported candidates are immutable", 409
            )
        candidate.status = CandidateStatus.REJECTED.value
        candidate.review_note = note
        candidate.reviewed_at = datetime.now(UTC)
        await self.session.flush()
        return candidate

    async def import_candidate(
        self, candidate_id: int
    ) -> DeviceProfileVersion:
        candidate = await self._candidate_for_update(candidate_id)
        if candidate.imported_profile_version_id is not None:
            profile = await self.session.get(
                DeviceProfileVersion, candidate.imported_profile_version_id
            )
            if profile is None:
                raise DomainError(
                    "candidate_import_inconsistent",
                    "Imported profile no longer exists",
                    409,
                )
            return profile
        if candidate.status != CandidateStatus.APPROVED.value:
            raise DomainError(
                "candidate_invalid", "Candidate must be approved before import", 409
            )
        candidate = await self._validate_locked(candidate)
        if candidate.status != CandidateStatus.VALID.value:
            raise DomainError(
                "candidate_invalid", "Candidate is no longer valid", 409
            )
        candidate.status = CandidateStatus.APPROVED.value
        payload = candidate.reviewed_payload
        async with self.session.begin_nested():
            profile = await self._import_graph(payload)
            run = await self.session.get(
                ProfileExtractionRun, candidate.extraction_run_id
            )
            candidate.status = CandidateStatus.IMPORTED.value
            candidate.imported_profile_version_id = profile.id
            candidate.imported_at = datetime.now(UTC)
            if run is not None:
                run.status = ExtractionRunStatus.COMPLETED.value
                run.finished_at = datetime.now(UTC)
            await self.session.flush()
        return profile

    async def _resolve_or_create_model(self, payload: dict[str, Any]) -> DeviceModel:
        existing_model_id = payload.get("existing_device_model_id")
        if existing_model_id is None:
            return await self.device_service.create_model(
                vendor_name=_sourced_value(payload["vendor_name"]),
                model_code=_sourced_value(payload["model_code"]),
                model_name=_sourced_value(payload["model_name"]),
                description=_sourced_value(payload.get("description")),
            )
        model = await self.session.get(DeviceModel, existing_model_id)
        if model is None:
            raise DomainError(
                "candidate_invalid", "Referenced DeviceModel does not exist", 409
            )
        if (
            model.vendor_name != _sourced_value(payload["vendor_name"])
            or model.model_code != _sourced_value(payload["model_code"])
        ):
            raise DomainError(
                "candidate_invalid",
                "Referenced DeviceModel conflicts with candidate identity",
                409,
            )
        return model

    async def _import_graph(self, payload: dict[str, Any]) -> DeviceProfileVersion:
        device_model = await self._resolve_or_create_model(payload["device_model"])
        profile_payload = payload["profile"]
        profile = await self.device_service.create_profile(
            device_model.id,
            version=_sourced_value(profile_payload["version"]),
            profile_name=_sourced_value(profile_payload.get("profile_name")),
            description=_sourced_value(profile_payload.get("description")),
        )
        protocols = {}
        for item in payload["protocols"]:
            protocol = await self.device_service.create_protocol_spec(
                profile.id,
                code=_sourced_value(item["code"]),
                protocol=_sourced_value(item["protocol"]),
                display_name=_sourced_value(item.get("display_name")),
                config_schema=item.get("config_schema", {}),
            )
            protocols[item["candidate_ref"]] = protocol
        points = {}
        for item in payload["points"]:
            point = await self.device_service.create_point(
                protocols[item["protocol_ref"]].id,
                code=_sourced_value(item["code"]),
                name=_sourced_value(item["name"]),
                description=_sourced_value(item.get("description")),
                address_text=_sourced_value(item.get("address_text")),
                object_type=_sourced_value(item.get("object_type")),
                data_type=_sourced_value(item["data_type"]),
                access_mode=_sourced_value(item["access_mode"]),
                unit=_sourced_value(item.get("unit")),
                address_spec=item.get("address_spec", {}),
                value_schema=item.get("value_schema", {}),
                expected_rule=item.get("expected_rule", {}),
            )
            points[item["candidate_ref"]] = point
        capabilities = {}
        for item in payload["capability_matches"]:
            capability = await device_repository.get_capability_by_code(
                self.session, item["capability_code"]
            )
            if capability is None:
                raise DomainError(
                    "candidate_invalid", "Capability resolution changed", 409
                )
            profile_capability, _ = await self.device_service.add_profile_capability(
                profile.id,
                capability.id,
                name_override=_sourced_value(item.get("name_override")),
            )
            capabilities[item["candidate_ref"]] = profile_capability
        for item in payload["point_bindings"]:
            await self.device_service.bind_capability_point(
                capabilities[item["capability_ref"]].id,
                points[item["point_ref"]].id,
                role=item["role"],
                alias=item.get("alias"),
                is_required=item.get("is_required", True),
            )
        return profile
