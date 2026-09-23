"""Task #58.14 autonomous retry lineage PostgreSQL migration coverage."""
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
from app.db.migration_adoption import verify_frozen_schema_at_head


POSTGRES_URL = os.getenv("TASK58_CATALOG_URL") or os.getenv("TASK58_POSTGRES_URL")
BACKEND = Path(__file__).resolve().parents[1]


def _admin_url(url: str) -> str:
    return make_url(url).set(database="postgres").render_as_string(
        hide_password=False
    )


@pytest.fixture
def isolated_database() -> Iterator[str]:
    if not POSTGRES_URL:
        pytest.skip("TASK58_POSTGRES_URL is required for Task #58.14 tests")
    name = f"task5814_{uuid4().hex[:16]}"
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


def _revision(url: str) -> str:
    engine = sa.create_engine(url)
    try:
        with engine.connect() as connection:
            return str(
                connection.execute(
                    sa.text("SELECT version_num FROM alembic_version")
                ).scalar_one()
            )
    finally:
        engine.dispose()


def _insert_generic_run(
    connection,
    *,
    table: str,
    row_id: str,
    status: str = "FAILED",
    stage: str | None = None,
) -> None:
    columns = connection.execute(
        sa.text(
            """SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name=:table
            ORDER BY ordinal_position"""
        ),
        {"table": table},
    ).all()
    names: list[str] = []
    values: list[str] = []
    for column, data_type, nullable in columns:
        names.append(f'"{column}"')
        if column == "id":
            values.append(f"'{row_id}'")
        elif column == "portfolio_item_id":
            values.append("'item-1'")
        elif column == "input_fingerprint":
            values.append("'" + "a" * 64 + "'")
        elif column == "status":
            values.append(f"'{status}'")
        elif column == "stage":
            values.append(f"'{stage or 'STARTED'}'")
        elif str(nullable) == "YES":
            values.append("NULL")
        elif data_type in {"character varying", "text"}:
            values.append("'x'")
        elif str(data_type).startswith("timestamp"):
            values.append("CURRENT_TIMESTAMP")
        elif data_type in {"integer", "numeric", "bigint"}:
            values.append("0")
        elif data_type == "boolean":
            values.append("false")
        elif data_type in {"json", "jsonb"}:
            values.append("'{}'::json")
        else:
            raise AssertionError(f"no generic value for {column} ({data_type})")
    connection.execute(sa.text("SET LOCAL session_replication_role = replica"))
    connection.execute(sa.text(
        f'INSERT INTO {table} ({", ".join(names)}) VALUES ({", ".join(values)})'
    ))
    connection.execute(sa.text("SET LOCAL session_replication_role = origin"))


def test_0029_to_0030_preserves_failed_rows_and_allows_superseding_attempts(
    isolated_database: str,
) -> None:
    _upgrade(isolated_database, "0029")
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            _insert_generic_run(
                connection,
                table="pinterest_autonomous_destination_runs",
                row_id="dest-1",
                stage="BOARD_READY",
            )
            _insert_generic_run(
                connection,
                table="pinterest_autonomous_execution_runs",
                row_id="exec-1",
                stage="SEO_READY",
            )
            _insert_generic_run(
                connection,
                table="pinterest_autonomous_generation_runs",
                row_id="gen-1",
            )
    finally:
        engine.dispose()

    _upgrade(isolated_database, "0030")
    assert _revision(isolated_database) == "0030"

    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            verify_frozen_schema_at_head(connection)
            for table, row_id in (
                ("pinterest_autonomous_destination_runs", "dest-1"),
                ("pinterest_autonomous_execution_runs", "exec-1"),
                ("pinterest_autonomous_generation_runs", "gen-1"),
            ):
                row = connection.execute(
                    sa.text(
                        f"SELECT id, portfolio_item_id, input_fingerprint, status, "
                        f"attempt_number, supersedes_run_id FROM {table} WHERE id=:id"
                    ),
                    {"id": row_id},
                ).one()
                assert row.id == row_id
                assert row.portfolio_item_id == "item-1"
                assert row.input_fingerprint == "a" * 64
                assert row.status == "FAILED"
                assert row.attempt_number == 1
                assert row.supersedes_run_id is None

            connection.execute(sa.text("SET LOCAL session_replication_role = replica"))
            connection.execute(sa.text(
                """INSERT INTO pinterest_autonomous_destination_runs
                (id, portfolio_item_id, plan_id, input_fingerprint, attempt_number,
                 supersedes_run_id, status, stage, safe_metadata, started_at)
                SELECT 'dest-2', portfolio_item_id, plan_id, input_fingerprint, 2,
                       id, 'STARTED', 'STARTED', '{}'::json, CURRENT_TIMESTAMP
                FROM pinterest_autonomous_destination_runs WHERE id='dest-1'"""
            ))
            connection.execute(sa.text(
                """INSERT INTO pinterest_autonomous_execution_runs
                (id, portfolio_item_id, plan_id, optimizer_application_id,
                 input_fingerprint, attempt_number, supersedes_run_id, status,
                 stage, scheduled_for, safe_metadata, started_at)
                SELECT 'exec-2', portfolio_item_id, plan_id, optimizer_application_id,
                       input_fingerprint, 2, id, 'STARTED', 'STARTED',
                       scheduled_for, '{}'::json, CURRENT_TIMESTAMP
                FROM pinterest_autonomous_execution_runs WHERE id='exec-1'"""
            ))
            connection.execute(sa.text(
                """INSERT INTO pinterest_autonomous_generation_runs
                (id, portfolio_item_id, seo_brief_id, input_fingerprint,
                 attempt_number, supersedes_run_id, status, safe_metadata, started_at)
                SELECT 'gen-2', portfolio_item_id, seo_brief_id, input_fingerprint,
                       2, id, 'STARTED', '{}'::json, CURRENT_TIMESTAMP
                FROM pinterest_autonomous_generation_runs WHERE id='gen-1'"""
            ))
            connection.execute(sa.text("SET LOCAL session_replication_role = origin"))

            for table, expected in (
                ("pinterest_autonomous_destination_runs", [("dest-1", 1, None), ("dest-2", 2, "dest-1")]),
                ("pinterest_autonomous_execution_runs", [("exec-1", 1, None), ("exec-2", 2, "exec-1")]),
                ("pinterest_autonomous_generation_runs", [("gen-1", 1, None), ("gen-2", 2, "gen-1")]),
            ):
                rows = connection.execute(
                    sa.text(
                        f"SELECT id, attempt_number, supersedes_run_id FROM {table} "
                        "WHERE portfolio_item_id='item-1' ORDER BY attempt_number"
                    )
                ).all()
                assert [tuple(row) for row in rows] == expected
    finally:
        engine.dispose()
