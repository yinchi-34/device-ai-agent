from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class DeviceInstanceCreate(BaseModel):
    device_model_id: int
    profile_version_id: int
    instance_code: str = Field(max_length=128)
    instance_name: str = Field(max_length=255)
    serial_number: str | None = None
    location: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DeviceInstanceOut(ORMModel):
    id: int
    device_model_id: int
    profile_version_id: int
    instance_code: str
    instance_name: str
    serial_number: str | None
    location: str | None
    status: str


class DeviceConnectionCreate(BaseModel):
    protocol_spec_id: int
    name: str = Field(max_length=128)
    config: dict[str, Any] = Field(default_factory=dict)
    secret_ref: str | None = None


class DeviceConnectionOut(ORMModel):
    id: int
    device_instance_id: int
    protocol_spec_id: int
    name: str
    config: dict[str, Any]
    status: str
    config_validated_at: datetime | None
    config_validation_error: dict[str, Any]


class DeviceContextOut(BaseModel):
    device: DeviceInstanceOut
    profile: dict[str, Any]
    protocol_specs: list[dict[str, Any]]
    connections: list[DeviceConnectionOut]


class ConnectionValidationOut(BaseModel):
    connection_id: int
    status: str
    errors: list[dict[str, Any]]


class CapabilityOptionOut(BaseModel):
    profile_capability_id: int
    code: str
    name: str
    points: list[dict[str, Any]]
    commands: list[str]


class PointOptionOut(BaseModel):
    id: int
    code: str
    name: str
    access_mode: str


class TestOptionsOut(BaseModel):
    capabilities: list[CapabilityOptionOut]
    points: list[PointOptionOut]
