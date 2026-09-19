"""Schema migrations. Run in the stack as `python -m booking.migrate` (ADR-0006).

Tests use the same functions, so the migrations they check are exactly
the ones the one-shot `booking-migrate` container applies.
"""

import os

from alembic import command
from alembic.config import Config


def alembic_config(database_url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", "booking:migrations")
    # Alembic config values use %-interpolation; a literal % in a password must be doubled.
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def upgrade(database_url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(database_url), revision)


def downgrade(database_url: str, revision: str) -> None:
    command.downgrade(alembic_config(database_url), revision)


def check(database_url: str) -> None:
    """Raise if the models describe a schema the migrations do not create."""
    command.check(alembic_config(database_url))


if __name__ == "__main__":
    upgrade(os.environ["SLOT_DATABASE_URL"])
