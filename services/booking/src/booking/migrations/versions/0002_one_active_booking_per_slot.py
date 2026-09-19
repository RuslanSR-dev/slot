"""Allow at most one active booking per slot (ADR-0005).

Revision ID: 0002
Revises: 0001

On a live table with data this would need duplicates cleaned up first and
CREATE INDEX CONCURRENTLY to avoid blocking writes. The table is new here.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"


def upgrade() -> None:
    op.create_index(
        "uq_bookings_active_slot",
        "bookings",
        ["slot_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'confirmed')"),
    )


def downgrade() -> None:
    op.drop_index("uq_bookings_active_slot", table_name="bookings")
