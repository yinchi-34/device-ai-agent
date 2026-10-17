from datetime import datetime
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


class ProfileCandidateFragment(CandidateModel):
    schema_version: Literal["1.0"]
    device_model: CandidateDeviceModel | None
    profile: CandidateProfile | None
    protocols: list[CandidateProtocol]
    points: list[CandidatePoint]
    capability_matches: list[CandidateCapabilityMatch]
    point_bindings: list[CandidatePointBinding]
    unresolved_suggestions: list[dict[str, Any]]


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class SourceDocumentOut(ApiModel):
    id: int
    device_model_id: int | None
    original_file_name: str
    media_type: str
    file_size_bytes: int
    file_hash_sha256: str
    file_metadata: dict[str, Any]
    validation_status: str
    created_at: datetime
    updated_at: datetime


class ExtractionRunOut(ApiModel):
    id: int
    source_document_id: int
    retry_of_run_id: int | None
    status: str
    attempt_number: int
    parser_version: str
    prompt_version: str
    candidate_schema_version: str
    extraction_provider: str
    extraction_model: str
    vision_provider: str | None
    vision_model: str | None
    embedding_provider: str
    embedding_model: str
    parse_metadata: dict[str, Any]
    usage_metadata: dict[str, Any]
    error_code: str | None
    error_detail: dict[str, Any]
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


class CandidateOut(ApiModel):
    id: int
    extraction_run_id: int
    original_payload: dict[str, Any]
    reviewed_payload: dict[str, Any]
    schema_errors: list[dict[str, Any]]
    domain_errors: list[dict[str, Any]]
    status: str
    revision: int
    review_note: str | None
    reviewed_at: datetime | None
    imported_profile_version_id: int | None
    imported_at: datetime | None
    created_at: datetime
    updated_at: datetime


class CandidatePatch(CandidateModel):
    revision: int = Field(ge=1)
    payload: dict[str, Any]


class CandidateAction(CandidateModel):
    revision: int = Field(ge=1)
    note: str | None = None


class CandidateImportOut(CandidateModel):
    profile_version_id: int


class DocumentSearchRequest(CandidateModel):
    query: str = Field(min_length=1, max_length=4000)
    source_document_id: int | None = None
    device_model_id: int | None = None
    limit: int = Field(default=10, ge=1, le=50)


class DocumentSearchResultOut(ApiModel):
    chunk_id: int
    source_document_id: int
    stable_ref: str
    page_number: int | None
    content: str
    similarity: float
    context_text: str = ""
