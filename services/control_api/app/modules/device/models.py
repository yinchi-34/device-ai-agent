from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, IdentityMixin, SoftDeleteMixin, TimestampMixin
from app.modules.device.enums import (
    AccessMode,
    CapabilityKind,
    ModelStatus,
    ProfileStatus,
)

_LIVE = text("is_deleted = false")


class DeviceCategory(IdentityMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "device_category"
    __table_args__ = (
        Index(
            "uq_device_category_name_live",
            func.lower(text("name")),
            unique=True,
            postgresql_where=_LIVE,
        ),
    )

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    models: Mapped[list["DeviceModel"]] = relationship(back_populates="category")
    category_capabilities: Mapped[list["DeviceCategoryCapability"]] = relationship(
        back_populates="category"
    )


class DeviceModel(IdentityMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "device_model"
    __table_args__ = (
        Index(
            "uq_device_model_vendor_code_live",
            "vendor_name",
            "model_code",
            unique=True,
            postgresql_where=_LIVE,
        ),
    )

    vendor_name: Mapped[str] = mapped_column(String(128), nullable=False)
    model_code: Mapped[str] = mapped_column(String(128), nullable=False)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    device_category_id: Mapped[int | None] = mapped_column(
        ForeignKey("device_category.id", ondelete="SET NULL")
    )
    description: Mapped[str | None] = mapped_column(Text)
    labels: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ModelStatus.DRAFT.value,
        server_default=ModelStatus.DRAFT.value,
    )

    category: Mapped[DeviceCategory | None] = relationship(back_populates="models")
    profiles: Mapped[list["DeviceProfileVersion"]] = relationship(
        back_populates="device_model"
    )


class BasicCapability(IdentityMixin, TimestampMixin, SoftDeleteMixin, Base):
    """Global, protocol-independent semantic capability."""

    __tablename__ = "basic_capability"
    __table_args__ = (
        Index(
            "uq_basic_capability_code_live",
            func.lower(text("code")),
            unique=True,
            postgresql_where=_LIVE,
        ),
    )

    code: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    capability_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default=CapabilityKind.OBSERVE.value,
        server_default=CapabilityKind.OBSERVE.value,
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default="{}"
    )

    category_capabilities: Mapped[list["DeviceCategoryCapability"]] = relationship(
        back_populates="basic_capability"
    )
    command_templates: Mapped[list["CapabilityCommandTemplate"]] = relationship(
        back_populates="basic_capability"
    )


class DeviceCategoryCapability(IdentityMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "device_category_capability"
    __table_args__ = (
        Index(
            "uq_category_capability_live",
            "device_category_id",
            "basic_capability_id",
            unique=True,
            postgresql_where=_LIVE,
        ),
    )

    device_category_id: Mapped[int] = mapped_column(
        ForeignKey("device_category.id"), nullable=False
    )
    basic_capability_id: Mapped[int] = mapped_column(
        ForeignKey("basic_capability.id"), nullable=False
    )
    is_recommended: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    category: Mapped[DeviceCategory] = relationship(
        back_populates="category_capabilities"
    )
    basic_capability: Mapped[BasicCapability] = relationship(
        back_populates="category_capabilities"
    )


class DeviceProfileVersion(IdentityMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "device_profile_version"
    __table_args__ = (
        UniqueConstraint("id", "device_model_id", name="uq_profile_id_model"),
        Index(
            "uq_profile_model_version_live",
            "device_model_id",
            "version",
            unique=True,
            postgresql_where=_LIVE,
        ),
        Index(
            "uq_profile_default_live",
            "device_model_id",
            unique=True,
            postgresql_where=text("is_default = true AND is_deleted = false"),
        ),
    )

    device_model_id: Mapped[int] = mapped_column(
        ForeignKey("device_model.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_name: Mapped[str | None] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ProfileStatus.DRAFT.value,
        server_default=ProfileStatus.DRAFT.value,
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    device_model: Mapped[DeviceModel] = relationship(back_populates="profiles")
    protocol_specs: Mapped[list["DeviceProtocolSpec"]] = relationship(
        back_populates="profile_version"
    )
    profile_capabilities: Mapped[list["ProfileCapability"]] = relationship(
        back_populates="profile_version"
    )
    commands: Mapped[list["DeviceCommand"]] = relationship(
        back_populates="profile_version"
    )


class DeviceProtocolSpec(IdentityMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "device_protocol_spec"
    __table_args__ = (
        Index(
            "uq_protocol_spec_profile_code_live",
            "profile_version_id",
            "code",
            unique=True,
            postgresql_where=_LIVE,
        ),
    )

    profile_version_id: Mapped[int] = mapped_column(
        ForeignKey("device_profile_version.id", ondelete="RESTRICT"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    protocol: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(128))
    config_schema: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default="{}"
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    profile_version: Mapped[DeviceProfileVersion] = relationship(
        back_populates="protocol_specs"
    )
    points: Mapped[list["DevicePoint"]] = relationship(
        back_populates="protocol_spec"
    )


class DevicePoint(IdentityMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "device_point"
    __table_args__ = (
        Index(
            "uq_device_point_spec_code_live",
            "protocol_spec_id",
            "code",
            unique=True,
            postgresql_where=_LIVE,
        ),
    )

    protocol_spec_id: Mapped[int] = mapped_column(
        ForeignKey("device_protocol_spec.id", ondelete="RESTRICT"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    semantic_type: Mapped[str | None] = mapped_column(String(64))
    address_text: Mapped[str | None] = mapped_column(String(128))
    object_type: Mapped[str | None] = mapped_column(String(64))
    data_type: Mapped[str] = mapped_column(String(64), nullable=False)
    access_mode: Mapped[str] = mapped_column(
        String(2), nullable=False, default=AccessMode.R.value,
        server_default=AccessMode.R.value,
    )
    unit: Mapped[str | None] = mapped_column(String(64))
    scale: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), nullable=False, default=1, server_default="1"
    )
    offset: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), nullable=False, default=0, server_default="0"
    )
    precision: Mapped[int | None] = mapped_column(SmallInteger)
    address_spec: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    value_schema: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    expected_rule: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    protocol_spec: Mapped[DeviceProtocolSpec] = relationship(back_populates="points")


class ProfileCapability(IdentityMixin, TimestampMixin, SoftDeleteMixin, Base):
    """How one concrete profile version implements a BasicCapability."""

    __tablename__ = "profile_capability"
    __table_args__ = (
        Index(
            "uq_profile_capability_live",
            "profile_version_id",
            "basic_capability_id",
            unique=True,
            postgresql_where=_LIVE,
        ),
    )

    profile_version_id: Mapped[int] = mapped_column(
        ForeignKey("device_profile_version.id"), nullable=False
    )
    basic_capability_id: Mapped[int] = mapped_column(
        ForeignKey("basic_capability.id"), nullable=False
    )
    name_override: Mapped[str | None] = mapped_column(String(255))
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default="{}"
    )

    profile_version: Mapped[DeviceProfileVersion] = relationship(
        back_populates="profile_capabilities"
    )
    basic_capability: Mapped[BasicCapability] = relationship()
    bindings: Mapped[list["CapabilityPointBinding"]] = relationship(
        back_populates="profile_capability"
    )


class CapabilityPointBinding(IdentityMixin, TimestampMixin, SoftDeleteMixin, Base):
    """Semantic M:N link between a ProfileCapability and a DevicePoint."""

    __tablename__ = "capability_point_binding"
    __table_args__ = (
        Index(
            "uq_binding_capability_point_role_live",
            "profile_capability_id",
            "device_point_id",
            "role",
            unique=True,
            postgresql_where=_LIVE,
        ),
    )

    profile_capability_id: Mapped[int] = mapped_column(
        ForeignKey("profile_capability.id"), nullable=False
    )
    device_point_id: Mapped[int] = mapped_column(
        ForeignKey("device_point.id"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    alias: Mapped[str | None] = mapped_column(String(128))
    is_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    binding_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    profile_capability: Mapped[ProfileCapability] = relationship(
        back_populates="bindings"
    )
    device_point: Mapped[DevicePoint] = relationship()


class CapabilityCommandTemplate(IdentityMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "capability_command_template"
    __table_args__ = (
        Index(
            "uq_command_template_capability_code_live",
            "basic_capability_id",
            "command_code",
            unique=True,
            postgresql_where=_LIVE,
        ),
    )

    basic_capability_id: Mapped[int] = mapped_column(
        ForeignKey("basic_capability.id"), nullable=False
    )
    command_code: Mapped[str] = mapped_column(String(128), nullable=False)
    command_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    access_mode: Mapped[str] = mapped_column(
        String(2), nullable=False, default=AccessMode.R.value,
        server_default=AccessMode.R.value,
    )
    operation_template: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    basic_capability: Mapped[BasicCapability] = relationship(
        back_populates="command_templates"
    )


class DeviceCommand(IdentityMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "device_command"
    __table_args__ = (
        Index(
            "uq_device_command_profile_code_live",
            "profile_version_id",
            "command_code",
            unique=True,
            postgresql_where=_LIVE,
        ),
    )

    profile_version_id: Mapped[int] = mapped_column(
        ForeignKey("device_profile_version.id"), nullable=False
    )
    profile_capability_id: Mapped[int] = mapped_column(
        ForeignKey("profile_capability.id"), nullable=False
    )
    source_template_id: Mapped[int | None] = mapped_column(
        ForeignKey("capability_command_template.id")
    )
    command_code: Mapped[str] = mapped_column(String(128), nullable=False)
    command_name: Mapped[str] = mapped_column(String(255), nullable=False)
    access_mode: Mapped[str] = mapped_column(
        String(2), nullable=False, default=AccessMode.R.value,
        server_default=AccessMode.R.value,
    )
    operations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    generated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    profile_version: Mapped[DeviceProfileVersion] = relationship(
        back_populates="commands"
    )
    profile_capability: Mapped[ProfileCapability] = relationship()
