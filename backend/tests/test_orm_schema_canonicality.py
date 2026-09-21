"""ORM metadata must reproduce the canonical 0023-0027 PostgreSQL catalogs."""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.db.base import Base
from app.db.migration_adoption import (
    FROZEN_FINGERPRINTS,
    POST_PUBLISH_DRIFT_TABLES,
    _catalog_contract,
    _fingerprint,
)
from app.models import domain  # noqa: F401


POSTGRES_URL = os.getenv("TASK58_CATALOG_URL") or os.getenv("TASK58_POSTGRES_URL")
BACKEND = Path(__file__).resolve().parents[1]


def _admin_url(url: str) -> str:
    return make_url(url).set(database="postgres").render_as_string(
        hide_password=False
    )


@pytest.fixture
def isolated_database() -> Iterator[str]:
    if not POSTGRES_URL:
        pytest.skip("TASK58_POSTGRES_URL is required for ORM canonicality tests")
    name = f"task583orm_{uuid4().hex[:16]}"
    admin = sa.create_engine(_admin_url(POSTGRES_URL), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
        yield make_url(POSTGRES_URL).set(database=name).render_as_string(
            hide_password=False
        )
    finally:
        with admin.connect() as connection:
            connection.execute(
                sa.text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname=:database AND pid <> pg_backend_pid()"
                ),
                {"database": name},
            )
            connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


@contextmanager
def _database_environment(url: str):
    previous = os.environ.get("DATABASE_URL")
    cwd = Path.cwd()
    os.environ["DATABASE_URL"] = url
    get_settings.cache_clear()
    os.chdir(BACKEND)
    try:
        yield
    finally:
        os.chdir(cwd)
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        get_settings.cache_clear()


def _upgrade(url: str, target: str) -> None:
    with _database_environment(url):
        command.upgrade(Config(str(BACKEND / "alembic.ini")), target)


def test_orm_metadata_matches_frozen_0023_0027_catalogs(
    isolated_database: str,
) -> None:
    # Create all dependencies canonically through 0022, then let SQLAlchemy ORM
    # metadata create exactly the six tables Replit's schema promotion sees.
    _upgrade(isolated_database, "0022")
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            Base.metadata.create_all(
                connection,
                tables=[
                    Base.metadata.tables[table]
                    for table in POST_PUBLISH_DRIFT_TABLES
                ],
            )
            actual = {
                table: _fingerprint(_catalog_contract(connection, table))
                for table in POST_PUBLISH_DRIFT_TABLES
            }
    finally:
        engine.dispose()

    expected = {
        table: FROZEN_FINGERPRINTS[table]
        for table in POST_PUBLISH_DRIFT_TABLES
    }
    assert actual == expected
