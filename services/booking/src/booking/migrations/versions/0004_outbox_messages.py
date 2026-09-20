"""Outbox for booking events.

Revision ID: 0004
Revises: 0003

The partial index covers only unpublished rows: the relay asks for those,
and published rows stay in the table as an audit trail without making the
index grow forever.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"


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


def downgrade() -> None:
    op.drop_index("ix_outbox_messages_unpublished", table_name="outbox_messages")
    op.drop_table("outbox_messages")
