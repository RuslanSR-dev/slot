"""Create slots and bookings.

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None


def upgrade() -> None:
    op.create_table(
        "slots",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("master_id", sa.String(64), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_slots_master_starts_at", "slots", ["master_id", "starts_at"])

    op.create_table(
        "bookings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("slot_id", sa.Uuid(), sa.ForeignKey("slots.id"), nullable=False),
        sa.Column("client_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'cancelled', 'expired')",
            name="ck_bookings_status",
        ),
    )
    op.create_index("ix_bookings_slot_id", "bookings", ["slot_id"])


def downgrade() -> None:
    op.drop_table("bookings")
    op.drop_table("slots")
