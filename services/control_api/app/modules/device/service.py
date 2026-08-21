from typing import Any

from jsonschema import Draft202012Validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.modules.device import repository
from app.modules.device.command_generation import generate_profile_commands
from app.modules.device.enums import PointRole, ProfileStatus
from app.modules.device.models import (
    BasicCapability,
    CapabilityCommandTemplate,
    CapabilityPointBinding,
    DeviceCategory,
    DeviceCategoryCapability,
    DeviceCommand,
    DeviceModel,
    DevicePoint,
    DeviceProfileVersion,
    DeviceProtocolSpec,
    ProfileCapability,
)


def _duplicate(message: str) -> DomainError:
    return DomainError("duplicate", message, status_code=409)


async def _flush(session: AsyncSession, message: str = "Duplicate live entity") -> None:
    try:
        await session.flush()
    except IntegrityError as exc:
        raise _duplicate(message) from exc


def _ensure_profile_mutable(profile: DeviceProfileVersion) -> None:
    if profile.status != ProfileStatus.DRAFT.value:
        raise DomainError(
            "profile_is_immutable",
            f"Profile {profile.id} is {profile.status}; configuration cannot be modified",
            status_code=409,
        )


class DeviceService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ catalog

    async def create_category(
        self, name: str, description: str | None = None
    ) -> DeviceCategory:
        if await repository.get_category_by_name(self.session, name):
            raise _duplicate(f"DeviceCategory {name!r} already exists")
        category = DeviceCategory(name=name, description=description)
        self.session.add(category)
        await _flush(self.session)
        return category

    async def create_model(
        self,
        vendor_name: str,
        model_code: str,
        model_name: str,
        device_category_id: int | None = None,
        description: str | None = None,
        labels: list[Any] | None = None,
    ) -> DeviceModel:
        if device_category_id is not None:
            category = await repository.get(
                self.session, DeviceCategory, device_category_id
            )
            if category is None:
                raise DomainError(
                    "not_found", f"DeviceCategory {device_category_id} not found", 404
                )
        model = DeviceModel(
            vendor_name=vendor_name,
            model_code=model_code,
            model_name=model_name,
            device_category_id=device_category_id,
            description=description,
            labels=labels or [],
        )
        self.session.add(model)
        await _flush(self.session, f"DeviceModel {vendor_name}/{model_code} exists")
        return model

    async def create_capability(
        self,
        code: str,
        name: str,
        description: str | None = None,
        capability_kind: str = "observe",
        metadata: dict[str, Any] | None = None,
    ) -> BasicCapability:
        if await repository.get_capability_by_code(self.session, code):
            raise _duplicate(f"BasicCapability {code!r} already exists")
        capability = BasicCapability(
            code=code,
            name=name,
            description=description,
            capability_kind=capability_kind,
            metadata_=metadata or {},
        )
        self.session.add(capability)
        await _flush(self.session)
        return capability

    async def associate_category_capability(
        self,
        category_id: int,
        capability_id: int,
        is_recommended: bool = True,
        sort_order: int = 0,
    ) -> DeviceCategoryCapability:
        if await repository.get(self.session, DeviceCategory, category_id) is None:
            raise DomainError("not_found", f"DeviceCategory {category_id} not found", 404)
        if await repository.get(self.session, BasicCapability, capability_id) is None:
            raise DomainError(
                "not_found", f"BasicCapability {capability_id} not found", 404
            )
        link = DeviceCategoryCapability(
            device_category_id=category_id,
            basic_capability_id=capability_id,
            is_recommended=is_recommended,
            sort_order=sort_order,
        )
        self.session.add(link)
        await _flush(self.session, "Capability already bound to category")
        return link

    # ------------------------------------------------------------------ profile

    async def create_profile(
        self,
        device_model_id: int,
        version: str,
        profile_name: str | None = None,
        description: str | None = None,
    ) -> DeviceProfileVersion:
        if await repository.get(self.session, DeviceModel, device_model_id) is None:
            raise DomainError(
                "not_found", f"DeviceModel {device_model_id} not found", 404
            )
        profile = DeviceProfileVersion(
            device_model_id=device_model_id,
            version=version,
            profile_name=profile_name,
            description=description,
        )
        self.session.add(profile)
        await _flush(self.session, f"Profile {version!r} already exists for model")
        return profile

    async def create_protocol_spec(
        self,
        profile_id: int,
        code: str,
        protocol: str,
        display_name: str | None = None,
        config_schema: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DeviceProtocolSpec:
        profile = await self.get_profile(profile_id)
        _ensure_profile_mutable(profile)
        schema = config_schema or {}
        if schema:
            try:
                Draft202012Validator.check_schema(schema)
            except Exception as exc:
                raise DomainError(
                    "invalid_config_schema",
                    f"config_schema is not valid JSON Schema: {exc}",
                    status_code=422,
                ) from exc
        spec = DeviceProtocolSpec(
            profile_version_id=profile.id,
            code=code,
            protocol=protocol,
            display_name=display_name,
            config_schema=schema,
            metadata_=metadata or {},
        )
        self.session.add(spec)
        await _flush(self.session, f"Protocol {code!r} already exists on profile")
        return spec

    async def create_point(
        self, protocol_spec_id: int, **fields: Any
    ) -> DevicePoint:
        spec = await repository.get(self.session, DeviceProtocolSpec, protocol_spec_id)
        if spec is None:
            raise DomainError(
                "not_found", f"DeviceProtocolSpec {protocol_spec_id} not found", 404
            )
        profile = await self.get_profile(spec.profile_version_id)
        fields.setdefault("data_type", "int16")
        _ensure_profile_mutable(profile)
        point = DevicePoint(protocol_spec_id=spec.id, **fields)
        self.session.add(point)
        await _flush(self.session, f"Point {fields.get('code')!r} exists on protocol")
        return point

    # ------------------------------------------------------------- capabilities

    async def add_profile_capability(
        self,
        profile_id: int,
        basic_capability_id: int,
        name_override: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[ProfileCapability, bool]:
        """Returns (profile_capability, outside_category_recommendation)."""
        profile = await self.get_profile(profile_id)
        _ensure_profile_mutable(profile)
        capability = await repository.get(
            self.session, BasicCapability, basic_capability_id
        )
        if capability is None:
            raise DomainError(
                "not_found", f"BasicCapability {basic_capability_id} not found", 404
            )
        outside = True
        if profile.device_model_id is not None:
            model = await repository.get(
                self.session, DeviceModel, profile.device_model_id
            )
            if model is not None and model.device_category_id is not None:
                stmt = select(DeviceCategoryCapability).where(
                    DeviceCategoryCapability.device_category_id
                    == model.device_category_id,
                    DeviceCategoryCapability.basic_capability_id == capability.id,
                    DeviceCategoryCapability.is_deleted.is_(False),
                )
                outside = await self.session.scalar(stmt) is None
        profile_capability = ProfileCapability(
            profile_version_id=profile.id,
            basic_capability_id=capability.id,
            name_override=name_override,
            metadata_=metadata or {},
        )
        self.session.add(profile_capability)
        await _flush(self.session, "Capability already implemented on this profile")
        return profile_capability, outside

    async def bind_capability_point(
        self,
        profile_capability_id: int,
        device_point_id: int,
        role: str,
        alias: str | None = None,
        is_required: bool = True,
        sort_order: int = 0,
        binding_config: dict[str, Any] | None = None,
    ) -> CapabilityPointBinding:
        if role not in {r.value for r in PointRole}:
            raise DomainError(
                "invalid_role", f"Unsupported binding role {role!r}", status_code=422
            )
        profile_capability = await repository.get(
            self.session, ProfileCapability, profile_capability_id
        )
        if profile_capability is None:
            raise DomainError(
                "not_found", f"ProfileCapability {profile_capability_id} not found", 404
            )
        profile = await self.get_profile(profile_capability.profile_version_id)
        _ensure_profile_mutable(profile)
        point = await repository.get(self.session, DevicePoint, device_point_id)
        if point is None:
            raise DomainError(
                "not_found", f"DevicePoint {device_point_id} not found", 404
            )
        spec = await repository.get(
            self.session, DeviceProtocolSpec, point.protocol_spec_id
        )
        if spec is None or spec.profile_version_id != profile.id:
            raise DomainError(
                "cross_profile_binding",
                "Bound DevicePoint must belong to the same ProfileVersion",
                status_code=409,
            )
        binding = CapabilityPointBinding(
            profile_capability_id=profile_capability.id,
            device_point_id=point.id,
            role=role,
            alias=alias,
            is_required=is_required,
            sort_order=sort_order,
            binding_config=binding_config or {},
        )
        self.session.add(binding)
        await _flush(self.session, "Same point already bound with this role")
        return binding

    async def create_command_template(
        self,
        basic_capability_id: int,
        command_code: str,
        command_name: str,
        operation_template: dict[str, Any],
        access_mode: str = "R",
        description: str | None = None,
    ) -> CapabilityCommandTemplate:
        capability = await repository.get(
            self.session, BasicCapability, basic_capability_id
        )
        if capability is None:
            raise DomainError(
                "not_found", f"BasicCapability {basic_capability_id} not found", 404
            )
        template = CapabilityCommandTemplate(
            basic_capability_id=capability.id,
            command_code=command_code,
            command_name=command_name,
            description=description,
            access_mode=access_mode,
            operation_template=operation_template,
        )
        from app.modules.device.command_generation import validate_command_template

        validate_command_template(template)
        self.session.add(template)
        await _flush(self.session, f"Template {command_code!r} exists on capability")
        return template

    # ------------------------------------------------------------------ release

    async def validate_profile_for_release(
        self, profile_id: int
    ) -> DeviceProfileVersion:
        profile = await repository.get_profile_with_graph(self.session, profile_id)
        if profile is None:
            raise DomainError("not_found", f"Profile {profile_id} not found", 404)
        if profile.status != ProfileStatus.DRAFT.value:
            raise DomainError(
                "profile_not_draft",
                f"Profile {profile.id} is {profile.status}; only draft can be released",
                status_code=409,
            )
        specs = [s for s in profile.protocol_specs if not s.is_deleted]
        if not specs:
            raise DomainError(
                "profile_incomplete", "Profile has no live ProtocolSpec", status_code=409
            )
        points = [p for s in specs for p in s.points if not p.is_deleted]
        if not points:
            raise DomainError(
                "profile_incomplete", "Profile has no live DevicePoint", status_code=409
            )
        for spec in specs:
            if spec.config_schema:
                try:
                    Draft202012Validator.check_schema(spec.config_schema)
                except Exception as exc:
                    raise DomainError(
                        "invalid_config_schema",
                        f"ProtocolSpec {spec.code} config_schema invalid: {exc}",
                        status_code=409,
                    ) from exc
        point_profile_ids = {p.protocol_spec.profile_version_id for p in points}
        for profile_capability in profile.profile_capabilities:
            if profile_capability.is_deleted:
                continue
            for binding in profile_capability.bindings:
                if binding.is_deleted:
                    continue
                spec = binding.device_point.protocol_spec
                if spec.profile_version_id != profile.id:
                    raise DomainError(
                        "cross_profile_binding",
                        f"Binding {binding.id} references a point outside the profile",
                        status_code=409,
                    )
        if point_profile_ids - {profile.id}:
            raise DomainError(
                "cross_profile_binding", "Profile contains foreign points", 409
            )
        # Raises on missing/ambiguous roles or invalid templates.
        generate_profile_commands(profile)
        return profile

    async def release_profile(
        self, profile_id: int, make_default: bool = False
    ) -> DeviceProfileVersion:
        profile = await self.validate_profile_for_release(profile_id)
        await self._upsert_generated_commands(profile)
        profile.status = ProfileStatus.RELEASED.value
        if make_default:
            stmt = select(DeviceProfileVersion).where(
                DeviceProfileVersion.device_model_id == profile.device_model_id,
                DeviceProfileVersion.is_default.is_(True),
                DeviceProfileVersion.id != profile.id,
            )
            for other in (await self.session.scalars(stmt)).all():
                other.is_default = False
            profile.is_default = True
        await _flush(self.session)
        return profile

    async def regenerate_commands(self, profile_id: int) -> list[DeviceCommand]:
        profile = await repository.get_profile_with_graph(self.session, profile_id)
        if profile is None:
            raise DomainError("not_found", f"Profile {profile_id} not found", 404)
        _ensure_profile_mutable(profile)
        commands = await self._upsert_generated_commands(profile)
        await _flush(self.session)
        return commands

    async def _upsert_generated_commands(
        self, profile: DeviceProfileVersion
    ) -> list[DeviceCommand]:
        generated = generate_profile_commands(profile)
        existing = {
            c.command_code: c
            for c in profile.commands
            if not c.is_deleted and c.generated
        }
        result: list[DeviceCommand] = []
        for command in generated:
            current = existing.pop(command.command_code, None)
            if current is None:
                self.session.add(command)
                result.append(command)
            else:
                current.operations = command.operations
                current.source_hash = command.source_hash
                current.command_name = command.command_name
                current.access_mode = command.access_mode
                current.profile_capability_id = command.profile_capability_id
                current.source_template_id = command.source_template_id
                result.append(current)
        for stale in existing.values():
            stale.is_deleted = True
        await self.session.flush()
        return result

    # ------------------------------------------------------------------ helpers

    async def get_profile(self, profile_id: int) -> DeviceProfileVersion:
        profile = await repository.get(
            self.session, DeviceProfileVersion, profile_id
        )
        if profile is None:
            raise DomainError("not_found", f"Profile {profile_id} not found", 404)
        return profile
