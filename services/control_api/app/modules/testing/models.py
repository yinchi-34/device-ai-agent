from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, IdentityMixin, SoftDeleteMixin, TimestampMixin
from app.modules.device.models import DeviceProfileVersion, DeviceProtocolSpec

_LIVE = text("is_deleted = false")


class DeviceInstance(IdentityMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "device_instance"
    __table_args__ = (
        ForeignKeyConstraint(
            ["profile_version_id", "device_model_id"],
            ["device_profile_version.id", "device_profile_version.device_model_id"],
            name="fk_instance_profile_model",
        ),
        Index(
            "uq_device_instance_code_live",
            "instance_code",
            unique=True,
            postgresql_where=_LIVE,
        ),
    )

    device_model_id: Mapped[int] = mapped_column(nullable=False)
    profile_version_id: Mapped[int] = mapped_column(nullable=False)
    instance_code: Mapped[str] = mapped_column(String(128), nullable=False)
    instance_name: Mapped[str] = mapped_column(String(255), nullable=False)
    serial_number: Mapped[str | None] = mapped_column(String(128))
    location: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active", server_default="active"
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_profile_validated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default="{}"
    )

    profile_version: Mapped[DeviceProfileVersion] = relationship()
    connections: Mapped[list["DeviceConnection"]] = relationship(
        back_populates="device_instance"
    )


class DeviceConnection(IdentityMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "device_connection"
    __table_args__ = (
        Index(
            "uq_device_connection_instance_name_live",
            "device_instance_id",
            "name",
            unique=True,
            postgresql_where=_LIVE,
        ),
    )

    device_instance_id: Mapped[int] = mapped_column(
        ForeignKey("device_instance.id"), nullable=False
    )
    protocol_spec_id: Mapped[int] = mapped_column(
        ForeignKey("device_protocol_spec.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    secret_ref: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="draft", server_default="draft"
    )
    config_validated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    config_validation_error: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    device_instance: Mapped[DeviceInstance] = relationship(
        back_populates="connections"
    )
    protocol_spec: Mapped[DeviceProtocolSpec] = relationship()
