"""Create payments and provider events.

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None


def upgrade() -> None:
    op.create_table(
        "payments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("booking_id", sa.Uuid(), nullable=False),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("provider_charge_id", sa.String(64), nullable=True),
        sa.Column("checkout_url", sa.String(512), nullable=True),
        sa.Column("needs_refund", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("booking_id", name="uq_payments_booking_id"),
        sa.CheckConstraint(
            "status IN ('failed', 'pending', 'succeeded')", name="ck_payments_status"
        ),
        sa.CheckConstraint("amount_minor > 0", name="ck_payments_amount_positive"),
    )
    op.create_table(
        "provider_events",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("provider_events")
    op.drop_table("payments")
