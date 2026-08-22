from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
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

from app.db.base import Base, IdentityMixin, SoftDeleteMixin, TimestampMixin
from app.modules.document.enums import (
    CandidateStatus,
    ChunkContentType,
    ExtractionMethod,
    ExtractionRunStatus,
    SourceDocumentStatus,
)


class SourceDocument(IdentityMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "source_document"
    __table_args__ = (Index("ix_source_document_hash", "file_hash_sha256"),)

    device_model_id: Mapped[int | None] = mapped_column(
        ForeignKey("device_model.id", ondelete="RESTRICT")
    )
    original_file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    file_hash_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    file_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    validation_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=SourceDocumentStatus.VALIDATED.value,
        server_default=SourceDocumentStatus.VALIDATED.value,
    )

    extraction_runs: Mapped[list["ProfileExtractionRun"]] = relationship(
        back_populates="source_document"
    )
    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="source_document"
    )


class ProfileExtractionRun(IdentityMixin, TimestampMixin, Base):
    __tablename__ = "profile_extraction_run"

    source_document_id: Mapped[int] = mapped_column(
        ForeignKey("source_document.id", ondelete="RESTRICT"), nullable=False
    )
    retry_of_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("profile_extraction_run.id", ondelete="RESTRICT")
    )
    celery_task_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ExtractionRunStatus.QUEUED.value,
        server_default=ExtractionRunStatus.QUEUED.value,
    )
    attempt_number: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    parser_version: Mapped[str] = mapped_column(
        String(32), nullable=False, default="1", server_default="1"
    )
    prompt_version: Mapped[str] = mapped_column(
        String(32), nullable=False, default="1", server_default="1"
    )
    candidate_schema_version: Mapped[str] = mapped_column(
        String(32), nullable=False, default="1", server_default="1"
    )
    extraction_provider: Mapped[str] = mapped_column(String(128), nullable=False)
    extraction_model: Mapped[str] = mapped_column(String(255), nullable=False)
    vision_provider: Mapped[str | None] = mapped_column(String(128))
    vision_model: Mapped[str | None] = mapped_column(String(255))
    embedding_provider: Mapped[str] = mapped_column(String(128), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(255), nullable=False)
    parse_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    raw_agent_response: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    usage_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    error_code: Mapped[str | None] = mapped_column(String(128))
    error_detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    source_document: Mapped[SourceDocument] = relationship(
        back_populates="extraction_runs"
    )
    retry_of_run: Mapped["ProfileExtractionRun | None"] = relationship(
        remote_side="ProfileExtractionRun.id"
    )
    candidate: Mapped["ProfileExtractionCandidate | None"] = relationship(
        back_populates="extraction_run", uselist=False
    )
    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="extraction_run"
    )


class ProfileExtractionCandidate(IdentityMixin, TimestampMixin, Base):
    __tablename__ = "profile_extraction_candidate"
    __table_args__ = (
        UniqueConstraint("extraction_run_id", name="uq_candidate_run"),
    )

    extraction_run_id: Mapped[int] = mapped_column(
        ForeignKey("profile_extraction_run.id", ondelete="RESTRICT"),
        nullable=False,
    )
    original_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    reviewed_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    schema_errors: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    domain_errors: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=CandidateStatus.PENDING.value,
        server_default=CandidateStatus.PENDING.value,
    )
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    review_note: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    imported_profile_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("device_profile_version.id", ondelete="SET NULL")
    )
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    extraction_run: Mapped[ProfileExtractionRun] = relationship(
        back_populates="candidate"
    )


class DocumentChunk(IdentityMixin, Base):
    __tablename__ = "document_chunk"
    __table_args__ = (
        UniqueConstraint(
            "extraction_run_id",
            "stable_ref",
            name="uq_document_chunk_run_ref",
        ),
    )

    source_document_id: Mapped[int] = mapped_column(
        ForeignKey("source_document.id", ondelete="RESTRICT"), nullable=False
    )
    extraction_run_id: Mapped[int] = mapped_column(
        ForeignKey("profile_extraction_run.id", ondelete="RESTRICT"),
        nullable=False,
    )
    stable_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ChunkContentType.TEXT.value
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer)
    block_index: Mapped[int] = mapped_column(Integer, nullable=False)
    bounding_box: Mapped[list[float] | None] = mapped_column(JSONB)
    table_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    extraction_method: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ExtractionMethod.PDF_TEXT.value
    )
    extraction_confidence: Mapped[float | None] = mapped_column(Float)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    source_document: Mapped[SourceDocument] = relationship(back_populates="chunks")
    extraction_run: Mapped[ProfileExtractionRun] = relationship(
        back_populates="chunks"
    )


class DocumentChunkEmbedding(IdentityMixin, TimestampMixin, Base):
    __tablename__ = "document_chunk_embedding"
    __table_args__ = (
        UniqueConstraint(
            "document_chunk_id",
            "provider",
            "model",
            name="uq_chunk_embedding_provider_model",
        ),
    )

    document_chunk_id: Mapped[int] = mapped_column(
        ForeignKey("document_chunk.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(), nullable=False)

    document_chunk: Mapped[DocumentChunk] = relationship()
