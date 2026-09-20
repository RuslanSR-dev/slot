"""Index for "my bookings".

Revision ID: 0005
Revises: 0004

The page of a client asks for every booking with their id. On a real table
that is a sequential scan; the index turns it into a lookup. Creating it on
a live table locks writes, so in production it would be CONCURRENTLY - here
the table is small and the stack is rebuilt from scratch.
"""

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"


def upgrade() -> None:
    op.create_index("ix_bookings_client_id", "bookings", ["client_id"])


def downgrade() -> None:
    op.drop_index("ix_bookings_client_id", table_name="bookings")
