"""Create notifications.

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("booking_id", sa.String(64), nullable=False),
        sa.Column("client_id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("text", sa.String(512), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.String(200), nullable=True),
        sa.Column("gateway_message_id", sa.String(64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("status IN ('dead', 'sending', 'sent')", name="ck_notifications_status"),
    )
    op.create_index("ix_notifications_booking_id", "notifications", ["booking_id"])


def downgrade() -> None:
    op.drop_index("ix_notifications_booking_id", table_name="notifications")
    op.drop_table("notifications")
