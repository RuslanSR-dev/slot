"""Migrations are code that runs once in production and cannot be retried
by hand at 3 a.m. They get the same checks as any other code."""

import re
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, inspect, make_url, text
from testcontainers.community.postgres import PostgresContainer

from booking import migrate
from booking.domain import ACTIVE_STATUSES
from booking.models import ACTIVE_SLOT_INDEX


@pytest.fixture
def empty_database_url(postgres: PostgresContainer) -> Iterator[str]:
    """A fresh database in the same Postgres, so the shared one is not touched."""
    server_url = postgres.get_connection_url()
    admin = create_engine(server_url, isolation_level="AUTOCOMMIT")
    name = f"migrations_{uuid.uuid4().hex[:8]}"
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    yield make_url(server_url).set(database=name).render_as_string(hide_password=False)
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    admin.dispose()


def table_names(database_url: str) -> set[str]:
    engine = create_engine(database_url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_migrations_upgrade_downgrade_and_upgrade_again(empty_database_url: str) -> None:
    migrate.upgrade(empty_database_url)
    assert {"slots", "bookings"} <= table_names(empty_database_url)

    migrate.downgrade(empty_database_url, "base")
    assert table_names(empty_database_url) == {"alembic_version"}

    # A downgrade that leaves debris usually breaks the next upgrade.
    migrate.upgrade(empty_database_url)
    assert {"slots", "bookings"} <= table_names(empty_database_url)


def test_schema_after_migrations_matches_models(database_url: str) -> None:
    """Fails when a model changed but nobody wrote the migration."""
    migrate.check(database_url)


def test_active_slot_index_covers_exactly_active_statuses(engine: Engine) -> None:
    """The set of active statuses lives in code and in the index (ADR-0005).

    Alembic does not compare index conditions, so this drift needs its own test.
    """
    with engine.connect() as connection:
        definition = connection.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"),
            {"name": ACTIVE_SLOT_INDEX},
        ).scalar_one()

    assert definition.startswith("CREATE UNIQUE INDEX")
    assert set(re.findall(r"'(\w+)'", definition)) == {str(s) for s in ACTIVE_STATUSES}
