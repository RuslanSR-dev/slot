import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, inspect, make_url, text
from testcontainers.community.postgres import PostgresContainer

from payments import migrate


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
    assert {"payments", "provider_events"} <= table_names(empty_database_url)

    migrate.downgrade(empty_database_url, "base")
    assert table_names(empty_database_url) == {"alembic_version"}

    migrate.upgrade(empty_database_url)
    assert {"payments", "provider_events"} <= table_names(empty_database_url)


def test_schema_after_migrations_matches_models(database_url: str) -> None:
    migrate.check(database_url)
