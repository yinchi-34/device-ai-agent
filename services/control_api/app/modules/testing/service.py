from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.modules.device import repository as device_repository
from app.modules.device.enums import ProfileStatus
from app.modules.device.models import (
    DeviceModel,
    DevicePoint,
    DeviceProfileVersion,
    DeviceProtocolSpec,
)
from app.modules.testing import repository
from app.modules.testing.config_validation import validate_connection_config
from app.modules.testing.models import DeviceConnection, DeviceInstance


class TestingService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_instance(
        self,
        device_model_id: int,
        profile_version_id: int,
        instance_code: str,
        instance_name: str,
        serial_number: str | None = None,
        location: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DeviceInstance:
        if (
            await device_repository.get(self.session, DeviceModel, device_model_id)
            is None
        ):
            raise DomainError(
                "not_found", f"DeviceModel {device_model_id} not found", 404
            )
        profile = await device_repository.get(
            self.session, DeviceProfileVersion, profile_version_id
        )
        if profile is None:
            raise DomainError(
                "not_found", f"Profile {profile_version_id} not found", 404
            )
        if profile.device_model_id != device_model_id:
            raise DomainError(
                "profile_model_mismatch",
                "Profile does not belong to the selected DeviceModel",
                status_code=409,
            )
        if profile.status != ProfileStatus.RELEASED.value:
            raise DomainError(
                "profile_not_released",
                "DeviceInstance can only bind a RELEASED profile",
                status_code=409,
            )
        instance = DeviceInstance(
            device_model_id=device_model_id,
            profile_version_id=profile_version_id,
            instance_code=instance_code,
            instance_name=instance_name,
            serial_number=serial_number,
            location=location,
            metadata_=metadata or {},
        )
        self.session.add(instance)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise DomainError(
                "duplicate", f"DeviceInstance {instance_code!r} already exists", 409
            ) from exc
        return instance

    async def create_connection(
        self,
        device_id: int,
        protocol_spec_id: int,
        name: str,
        config: dict[str, Any] | None = None,
        secret_ref: str | None = None,
    ) -> DeviceConnection:
        instance = await repository.get_instance(self.session, device_id)
        if instance is None:
            raise DomainError("not_found", f"DeviceInstance {device_id} not found", 404)
        spec = await device_repository.get(
            self.session, DeviceProtocolSpec, protocol_spec_id
        )
        if spec is None:
            raise DomainError(
                "not_found", f"DeviceProtocolSpec {protocol_spec_id} not found", 404
            )
        if spec.profile_version_id != instance.profile_version_id:
            raise DomainError(
                "protocol_profile_mismatch",
                "ProtocolSpec does not belong to the instance's current profile",
                status_code=409,
            )
        connection = DeviceConnection(
            device_instance_id=instance.id,
            protocol_spec_id=spec.id,
            name=name,
            config=config or {},
            secret_ref=secret_ref,
        )
        self.session.add(connection)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise DomainError(
                "duplicate", f"Connection {name!r} already exists on device", 409
            ) from exc
        return connection

    async def validate_connection(self, connection_id: int) -> DeviceConnection:
        """Structural-only validation; persists the verdict. No network I/O."""
        connection = await repository.get_connection(self.session, connection_id)
        if connection is None:
            raise DomainError(
                "not_found", f"DeviceConnection {connection_id} not found", 404
            )
        errors = validate_connection_config(
            connection.protocol_spec.config_schema or {}, connection.config or {}
        )
        connection.status = "invalid" if errors else "valid"
        connection.config_validation_error = {"errors": errors} if errors else {}
        connection.config_validated_at = datetime.now(UTC)
        await self.session.flush()
        return connection

    async def get_device_context(self, device_id: int) -> DeviceInstance:
        instance = await repository.get_instance(self.session, device_id)
        if instance is None:
            raise DomainError("not_found", f"DeviceInstance {device_id} not found", 404)
        return instance

    async def get_test_options(self, device_id: int) -> dict[str, Any]:
        instance = await repository.get_instance(self.session, device_id)
        if instance is None:
            raise DomainError("not_found", f"DeviceInstance {device_id} not found", 404)
        profile_id = instance.profile_version_id
        capabilities = await device_repository.list_profile_capabilities(
            self.session, profile_id
        )
        commands = await device_repository.list_profile_commands(
            self.session, profile_id
        )
        points = await device_repository.list_profile_points(self.session, profile_id)

        capability_items: list[dict[str, Any]] = []
        for pc in capabilities:
            capability = pc.basic_capability
            capability_items.append(
                {
                    "profile_capability_id": pc.id,
                    "code": capability.code,
                    "name": pc.name_override or capability.name,
                    "points": [
                        {
                            "code": b.device_point.code,
                            "role": b.role,
                            "access_mode": b.device_point.access_mode,
                        }
                        for b in sorted(
                            (b for b in pc.bindings if not b.is_deleted),
                            key=lambda b: (b.sort_order, b.id),
                        )
                    ],
                    "commands": [
                        c.command_code
                        for c in commands
                        if c.profile_capability_id == pc.id
                    ],
                }
            )
        return {
            "capabilities": capability_items,
            "points": [
                {
                    "id": p.id,
                    "code": p.code,
                    "name": p.name,
                    "access_mode": p.access_mode,
                }
                for p in points
            ],
        }

    async def list_points(self, device_id: int) -> list[DevicePoint]:
        instance = await repository.get_instance(self.session, device_id)
        if instance is None:
            raise DomainError("not_found", f"DeviceInstance {device_id} not found", 404)
        return list(
            await device_repository.list_profile_points(
                self.session, instance.profile_version_id
            )
        )
