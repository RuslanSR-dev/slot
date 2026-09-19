"""Slots have a price.

Revision ID: 0003
Revises: 0002

A NOT NULL column on a table that may already have rows needs a default,
otherwise the migration fails on existing data. New slots must set a real
price: the API rejects zero.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"


def upgrade() -> None:
    op.add_column(
        "slots",
        sa.Column("price_minor", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("slots", "price_minor")
