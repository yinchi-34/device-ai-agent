"""add document chunk context

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "document_chunk",
        sa.Column("context_text", sa.Text(), server_default="", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("document_chunk", "context_text")
