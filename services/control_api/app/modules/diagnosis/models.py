from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, IdentityMixin, TimestampMixin


class DiagnosisRun(IdentityMixin, TimestampMixin, Base):
    __tablename__ = "diagnosis_run"
    __table_args__ = (Index("ix_diagnosis_run_device_status", "device_id", "status"),)

    device_id: Mapped[int] = mapped_column(
        ForeignKey("device_instance.id", ondelete="RESTRICT"), nullable=False
    )
    symptom: Mapped[str] = mapped_column(Text, nullable=False)
    target_point_code: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="ready", server_default="ready"
    )
    model_provider: Mapped[str] = mapped_column(String(128), nullable=False)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    max_steps: Mapped[int] = mapped_column(
        Integer, nullable=False, default=8, server_default="8"
    )
    current_step_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    final_report: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    usage_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    error_code: Mapped[str | None] = mapped_column(String(128))
    error_detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    steps: Mapped[list["DiagnosisStep"]] = relationship(
        back_populates="run", order_by="DiagnosisStep.sequence_number"
    )


class DiagnosisStep(IdentityMixin, Base):
    __tablename__ = "diagnosis_step"
    __table_args__ = (
        UniqueConstraint(
            "diagnosis_run_id", "sequence_number", name="uq_diagnosis_step_sequence"
        ),
    )

    diagnosis_run_id: Mapped[int] = mapped_column(
        ForeignKey("diagnosis_run.id", ondelete="CASCADE"), nullable=False
    )
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    rationale: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    result: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    usage_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    run: Mapped[DiagnosisRun] = relationship(back_populates="steps")
