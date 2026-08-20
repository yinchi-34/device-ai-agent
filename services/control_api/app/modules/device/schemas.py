from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------- catalog


class DeviceCategoryCreate(BaseModel):
    name: str = Field(max_length=100)
    description: str | None = None


class DeviceCategoryOut(ORMModel):
    id: int
    name: str
    description: str | None


class DeviceModelCreate(BaseModel):
    vendor_name: str = Field(max_length=128)
    model_code: str = Field(max_length=128)
    model_name: str = Field(max_length=255)
    device_category_id: int | None = None
    description: str | None = None
    labels: list[Any] = Field(default_factory=list)


class DeviceModelOut(ORMModel):
    id: int
    vendor_name: str
    model_code: str
    model_name: str
    device_category_id: int | None
    description: str | None
    labels: list[Any]
    status: str


class BasicCapabilityCreate(BaseModel):
    code: str = Field(max_length=128)
    name: str = Field(max_length=255)
    description: str | None = None
    capability_kind: str = "observe"
    metadata: dict[str, Any] = Field(default_factory=dict)


class BasicCapabilityOut(ORMModel):
    id: int
    code: str
    name: str
    description: str | None
    capability_kind: str


class CategoryCapabilityLinkOut(ORMModel):
    id: int
    device_category_id: int
    basic_capability_id: int
    is_recommended: bool
    sort_order: int


# --------------------------------------------------------------------- profile


class ProfileCreate(BaseModel):
    version: str = Field(max_length=64)
    profile_name: str | None = Field(default=None, max_length=128)
    description: str | None = None


class ProfileOut(ORMModel):
    id: int
    device_model_id: int
    version: str
    profile_name: str | None
    description: str | None
    status: str
    is_default: bool


class ProtocolSpecCreate(BaseModel):
    code: str = Field(max_length=64)
    protocol: str
    display_name: str | None = Field(default=None, max_length=128)
    config_schema: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProtocolSpecOut(ORMModel):
    id: int
    profile_version_id: int
    code: str
    protocol: str
    display_name: str | None
    config_schema: dict[str, Any]
    is_default: bool


class PointCreate(BaseModel):
    code: str = Field(max_length=128)
    name: str = Field(max_length=255)
    description: str | None = None
    semantic_type: str | None = None
    address_text: str | None = None
    object_type: str | None = None
    data_type: str = "int16"
    access_mode: str = "R"
    unit: str | None = None
    scale: float = 1
    offset: float = 0
    precision: int | None = None
    address_spec: dict[str, Any] = Field(default_factory=dict)
    value_schema: dict[str, Any] = Field(default_factory=dict)
    expected_rule: dict[str, Any] = Field(default_factory=dict)
    sort_order: int = 0


class PointOut(ORMModel):
    id: int
    protocol_spec_id: int
    code: str
    name: str
    semantic_type: str | None
    address_text: str | None
    data_type: str
    access_mode: str
    unit: str | None
    sort_order: int


# ----------------------------------------------------------------- capabilities


class ProfileCapabilityCreate(BaseModel):
    name_override: str | None = Field(default=None, max_length=255)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProfileCapabilityOut(ORMModel):
    id: int
    profile_version_id: int
    basic_capability_id: int
    name_override: str | None


class ProfileCapabilityCreated(BaseModel):
    profile_capability: ProfileCapabilityOut
    outside_category_recommendation: bool


class BindingCreate(BaseModel):
    device_point_id: int
    role: str
    alias: str | None = Field(default=None, max_length=128)
    is_required: bool = True
    sort_order: int = 0
    binding_config: dict[str, Any] = Field(default_factory=dict)


class BindingOut(ORMModel):
    id: int
    profile_capability_id: int
    device_point_id: int
    role: str
    alias: str | None
    is_required: bool
    sort_order: int


class CommandTemplateCreate(BaseModel):
    command_code: str = Field(max_length=128)
    command_name: str = Field(max_length=255)
    operation_template: dict[str, Any]
    access_mode: str = "R"
    description: str | None = None


class CommandTemplateOut(ORMModel):
    id: int
    basic_capability_id: int
    command_code: str
    command_name: str
    access_mode: str
    operation_template: dict[str, Any]


class CommandOut(ORMModel):
    id: int
    profile_version_id: int
    profile_capability_id: int
    source_template_id: int | None
    command_code: str
    command_name: str
    access_mode: str
    operations: list[dict[str, Any]]
    source_hash: str
    generated: bool


class ReleaseRequest(BaseModel):
    make_default: bool = False


class ReleaseResponse(BaseModel):
    profile: ProfileOut
    generated_commands: list[str]


class ErrorOut(BaseModel):
    code: str
    message: str
