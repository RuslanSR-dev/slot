"""Outbox for commands to neighbours, and the result of a refund.

Revision ID: 0002
Revises: 0001

The webhook used to call booking inside its transaction. It now writes a
command here and commits; the relay delivers it (ADR-0010).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"


def upgrade() -> None:
    op.create_table(
        "outbox_messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("topic", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.String(200), nullable=True),
    )
    op.create_index(
        "ix_outbox_messages_unpublished",
        "outbox_messages",
        ["id"],
        postgresql_where=sa.text("published_at IS NULL"),
    )
    op.add_column("payments", sa.Column("provider_refund_id", sa.String(64), nullable=True))
    op.add_column("payments", sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("payments", "refunded_at")
    op.drop_column("payments", "provider_refund_id")
    op.drop_index("ix_outbox_messages_unpublished", table_name="outbox_messages")
    op.drop_table("outbox_messages")
