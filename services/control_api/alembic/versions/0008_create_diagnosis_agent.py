"""create read-only diagnosis agent audit tables

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "diagnosis_run",
        sa.Column("device_id", sa.BigInteger(), nullable=False),
        sa.Column("symptom", sa.Text(), nullable=False),
        sa.Column("target_point_code", sa.String(length=128), nullable=True),
        sa.Column(
            "status", sa.String(length=32), server_default="ready", nullable=False
        ),
        sa.Column("model_provider", sa.String(length=128), nullable=False),
        sa.Column("model_name", sa.String(length=255), nullable=False),
        sa.Column("max_steps", sa.Integer(), server_default="8", nullable=False),
        sa.Column(
            "current_step_count", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "final_report",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "usage_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column(
            "error_detail",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["device_id"], ["device_instance.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_diagnosis_run_device_status", "diagnosis_run", ["device_id", "status"]
    )
    op.create_table(
        "diagnosis_step",
        sa.Column("diagnosis_run_id", sa.BigInteger(), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column(
            "arguments",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("rationale", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "result",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "usage_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(
            ["diagnosis_run_id"], ["diagnosis_run.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "diagnosis_run_id", "sequence_number", name="uq_diagnosis_step_sequence"
        ),
    )


def downgrade() -> None:
    op.drop_table("diagnosis_step")
    op.drop_index("ix_diagnosis_run_device_status", table_name="diagnosis_run")
    op.drop_table("diagnosis_run")
