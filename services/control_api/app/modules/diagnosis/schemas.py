from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DiagnosisRunCreate(BaseModel):
    device_id: int
    symptom: str = Field(min_length=1, max_length=4000)
    target_point_code: str | None = Field(default=None, max_length=128)
    max_steps: int = Field(default=8, ge=1, le=20)


class DiagnosisStepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sequence_number: int
    tool_name: str
    arguments: dict[str, Any]
    rationale: str
    result: dict[str, Any]
    usage_metadata: dict[str, Any]
    created_at: datetime


class DiagnosisRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    device_id: int
    symptom: str
    target_point_code: str | None
    status: str
    model_provider: str
    model_name: str
    max_steps: int
    current_step_count: int
    final_report: dict[str, Any]
    usage_metadata: dict[str, Any]
    error_code: str | None
    error_detail: dict[str, Any]
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    steps: list[DiagnosisStepOut]
