from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class CandidateModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(CandidateModel):
    confidence: float = Field(ge=0, le=1)
    evidence_refs: list[str] = Field(min_length=1)


class SourcedString(Evidence):
    value: str


class CandidateDeviceModel(CandidateModel):
    existing_device_model_id: int | None = None
    vendor_name: SourcedString
    model_code: SourcedString
    model_name: SourcedString
    description: SourcedString | None = None


class CandidateProfile(CandidateModel):
    version: SourcedString
    profile_name: SourcedString | None = None
    description: SourcedString | None = None


class CandidateProtocol(CandidateModel):
    candidate_ref: str
    code: SourcedString
    protocol: SourcedString
    display_name: SourcedString | None = None
    config_schema: dict[str, Any] = Field(default_factory=dict)


class CandidatePoint(CandidateModel):
    candidate_ref: str
    protocol_ref: str
    code: SourcedString
    name: SourcedString
    description: SourcedString | None = None
    address_text: SourcedString | None = None
    object_type: SourcedString | None = None
    data_type: SourcedString
    access_mode: SourcedString
    unit: SourcedString | None = None
    address_spec: dict[str, Any] = Field(default_factory=dict)
    value_schema: dict[str, Any] = Field(default_factory=dict)
    expected_rule: dict[str, Any] = Field(default_factory=dict)


class CandidateCapabilityMatch(Evidence):
    candidate_ref: str
    capability_code: str
    name_override: SourcedString | None = None


class CandidatePointBinding(Evidence):
    capability_ref: str
    point_ref: str
    role: str
    alias: str | None = None
    is_required: bool = True


class ProfileCandidatePayload(CandidateModel):
    schema_version: Literal["1.0"]
    device_model: CandidateDeviceModel
    profile: CandidateProfile
    protocols: list[CandidateProtocol] = Field(min_length=1)
    points: list[CandidatePoint]
    capability_matches: list[CandidateCapabilityMatch]
    point_bindings: list[CandidatePointBinding]
    unresolved_suggestions: list[dict[str, Any]] = Field(default_factory=list)
