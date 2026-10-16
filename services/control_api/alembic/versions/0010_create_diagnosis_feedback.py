"""create diagnosis feedback

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "diagnosis_feedback",
        sa.Column("diagnosis_run_id", sa.BigInteger(), nullable=False),
        sa.Column("verdict", sa.String(length=32), nullable=False),
        sa.Column("expected_status", sa.String(length=32), nullable=False),
        sa.Column("expected_classification", sa.String(length=32), nullable=True),
        sa.Column("reviewer", sa.String(length=255), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
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
    )
    op.create_index(
        "ix_diagnosis_feedback_run_created",
        "diagnosis_feedback",
        ["diagnosis_run_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_diagnosis_feedback_run_created", table_name="diagnosis_feedback"
    )
    op.drop_table("diagnosis_feedback")
