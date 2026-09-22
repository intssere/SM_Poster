"""Task #58.6 exact residual head check-rendering repair."""
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

import app.db.migration_adoption as migration_adoption
from app.core.config import get_settings
from app.db.migration_adoption import (
    FROZEN_FINGERPRINTS,
    HEAD_EXECUTION_CHECK_RENDERING_DRIFT_FINGERPRINT,
    HEAD_EXECUTION_CHECK_RENDERING_TABLE,
    SchemaAdoptionRefused,
    _catalog_contract,
    reconcile_head_execution_check_rendering,
    verify_frozen_schema_at_head,
)


POSTGRES_URL = os.getenv("TASK58_CATALOG_URL") or os.getenv("TASK58_POSTGRES_URL")
BACKEND = Path(__file__).resolve().parents[1]
REAL_FINGERPRINT = migration_adoption._fingerprint


def _admin_url(url: str) -> str:
    return make_url(url).set(database="postgres").render_as_string(
        hide_password=False
    )


@pytest.fixture
def isolated_database() -> Iterator[str]:
    if not POSTGRES_URL:
        pytest.skip("TASK58_POSTGRES_URL is required for Task #58.6 tests")
    name = f"task586_{uuid4().hex[:16]}"
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


def _fingerprint(connection) -> str:
    return REAL_FINGERPRINT(
        _catalog_contract(connection, HEAD_EXECUTION_CHECK_RENDERING_TABLE)
    )


def _table_oid(connection) -> int:
    return int(
        connection.execute(
            sa.text(
                "SELECT 'public.pinterest_autonomous_execution_runs'::regclass::oid"
            )
        ).scalar_one()
    )


def _check_oids(connection) -> dict[str, int]:
    rows = connection.execute(
        sa.text(
            """SELECT conname, oid
            FROM pg_constraint
            WHERE conrelid =
                'public.pinterest_autonomous_execution_runs'::regclass
              AND conname IN (
                'ck_pinterest_auto_exec_stage',
                'ck_pinterest_auto_exec_status'
              )
            ORDER BY conname"""
        )
    ).all()
    return {str(name): int(oid) for name, oid in rows}


def _install_exact_residual(url: str) -> str:
    engine = sa.create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(
                """ALTER TABLE pinterest_autonomous_execution_runs
                DROP CONSTRAINT ck_pinterest_auto_exec_stage"""
            ))
            connection.execute(sa.text(
                """ALTER TABLE pinterest_autonomous_execution_runs
                DROP CONSTRAINT ck_pinterest_auto_exec_status"""
            ))
            connection.execute(sa.text(
                """ALTER TABLE pinterest_autonomous_execution_runs
                ADD CONSTRAINT ck_pinterest_auto_exec_status
                CHECK (
                  status::text = ANY (
                    ARRAY[
                      ('STARTED'::character varying)::text,
                      ('SUCCEEDED'::character varying)::text,
                      ('FAILED'::character varying)::text
                    ]
                  )
                )"""
            ))
            connection.execute(sa.text(
                """ALTER TABLE pinterest_autonomous_execution_runs
                ADD CONSTRAINT ck_pinterest_auto_exec_stage
                CHECK (
                  stage::text = ANY (
                    ARRAY[
                      ('STARTED'::character varying)::text,
                      ('SEO_READY'::character varying)::text,
                      ('GENERATED'::character varying)::text,
                      ('AUTHORIZED'::character varying)::text,
                      ('PUBLICATION_CREATED'::character varying)::text,
                      ('PERMITTED'::character varying)::text
                    ]
                  )
                )"""
            ))
            return _fingerprint(connection)
    finally:
        engine.dispose()


def _insert_target_row(connection) -> None:
    columns = connection.execute(
        sa.text(
            """SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema='public'
              AND table_name='pinterest_autonomous_execution_runs'
            ORDER BY ordinal_position"""
        )
    ).all()
    names: list[str] = []
    values: list[str] = []
    for column, data_type, nullable in columns:
        names.append(f'"{column}"')
        if column == "status":
            values.append("'STARTED'")
        elif column == "stage":
            values.append("'STARTED'")
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
        f'INSERT INTO pinterest_autonomous_execution_runs '
        f'({", ".join(names)}) VALUES ({", ".join(values)})'
    ))


def test_fresh_upgrade_reaches_0029_and_is_canonical(
    isolated_database: str,
) -> None:
    _upgrade(isolated_database, "head")
    assert _revision(isolated_database) == "0029"
    engine = sa.create_engine(isolated_database)
    try:
        with engine.connect() as connection:
            assert _fingerprint(connection) == FROZEN_FINGERPRINTS[
                HEAD_EXECUTION_CHECK_RENDERING_TABLE
            ]
            verify_frozen_schema_at_head(connection)
    finally:
        engine.dispose()


def test_canonical_0028_to_0029_is_schema_noop(
    isolated_database: str,
) -> None:
    _upgrade(isolated_database, "0028")
    engine = sa.create_engine(isolated_database)
    try:
        with engine.connect() as connection:
            table_oid = _table_oid(connection)
            check_oids = _check_oids(connection)
            before = _fingerprint(connection)
    finally:
        engine.dispose()

    _upgrade(isolated_database, "0029")
    assert _revision(isolated_database) == "0029"

    engine = sa.create_engine(isolated_database)
    try:
        with engine.connect() as connection:
            assert _table_oid(connection) == table_oid
            assert _check_oids(connection) == check_oids
            assert _fingerprint(connection) == before
    finally:
        engine.dispose()


def test_exact_residual_repairs_only_checks_and_preserves_incoming_fk(
    isolated_database: str,
) -> None:
    _upgrade(isolated_database, "0028")
    residual = _install_exact_residual(isolated_database)
    assert residual == HEAD_EXECUTION_CHECK_RENDERING_DRIFT_FINGERPRINT

    engine = sa.create_engine(isolated_database)
    try:
        with engine.connect() as connection:
            table_oid = _table_oid(connection)
            incoming_before = connection.execute(sa.text(
                """SELECT conname, pg_get_constraintdef(oid, true)
                FROM pg_constraint
                WHERE conrelid =
                    'public.pinterest_autonomous_destination_runs'::regclass
                  AND confrelid =
                    'public.pinterest_autonomous_execution_runs'::regclass
                ORDER BY conname"""
            )).all()
    finally:
        engine.dispose()

    _upgrade(isolated_database, "0029")
    assert _revision(isolated_database) == "0029"

    engine = sa.create_engine(isolated_database)
    try:
        with engine.connect() as connection:
            assert _table_oid(connection) == table_oid
            assert _fingerprint(connection) == FROZEN_FINGERPRINTS[
                HEAD_EXECUTION_CHECK_RENDERING_TABLE
            ]
            incoming_after = connection.execute(sa.text(
                """SELECT conname, pg_get_constraintdef(oid, true)
                FROM pg_constraint
                WHERE conrelid =
                    'public.pinterest_autonomous_destination_runs'::regclass
                  AND confrelid =
                    'public.pinterest_autonomous_execution_runs'::regclass
                ORDER BY conname"""
            )).all()
            assert incoming_after == incoming_before
    finally:
        engine.dispose()


def test_nonempty_exact_residual_refuses(
    isolated_database: str,
) -> None:
    _upgrade(isolated_database, "0028")
    assert _install_exact_residual(
        isolated_database
    ) == HEAD_EXECUTION_CHECK_RENDERING_DRIFT_FINGERPRINT
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            _insert_target_row(connection)
    finally:
        engine.dispose()

    with pytest.raises(SchemaAdoptionRefused, match="is not empty"):
        _upgrade(isolated_database, "0029")
    assert _revision(isolated_database) == "0028"


def test_unknown_fingerprint_refuses(isolated_database: str) -> None:
    _upgrade(isolated_database, "0028")
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(
                'ALTER INDEX ix_pinterest_auto_exec_status '
                'RENAME TO task586_unknown_status'
            ))
    finally:
        engine.dispose()

    with pytest.raises(
        SchemaAdoptionRefused,
        match="not the exact Task #58.6 fingerprint",
    ):
        _upgrade(isolated_database, "0029")
    assert _revision(isolated_database) == "0028"


def test_lock_recheck_refuses_drift(
    isolated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _upgrade(isolated_database, "0028")
    real = migration_adoption._table_fingerprints
    calls = 0

    def changing(connection, tables):
        nonlocal calls
        if tuple(tables) == (HEAD_EXECUTION_CHECK_RENDERING_TABLE,):
            calls += 1
            if calls == 1:
                return {
                    HEAD_EXECUTION_CHECK_RENDERING_TABLE:
                        HEAD_EXECUTION_CHECK_RENDERING_DRIFT_FINGERPRINT
                }
            return {HEAD_EXECUTION_CHECK_RENDERING_TABLE: "f" * 64}
        return real(connection, tables)

    monkeypatch.setattr(migration_adoption, "_table_fingerprints", changing)
    with pytest.raises(
        SchemaAdoptionRefused,
        match="fingerprint changed while locking",
    ):
        _upgrade(isolated_database, "0029")
    assert _revision(isolated_database) == "0028"


def test_post_repair_mismatch_rolls_back(
    isolated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _upgrade(isolated_database, "0028")
    assert _install_exact_residual(
        isolated_database
    ) == HEAD_EXECUTION_CHECK_RENDERING_DRIFT_FINGERPRINT
    monkeypatch.setitem(
        FROZEN_FINGERPRINTS,
        HEAD_EXECUTION_CHECK_RENDERING_TABLE,
        "1" * 64,
    )

    with pytest.raises(
        SchemaAdoptionRefused,
        match="canonical predecessor contracts",
    ):
        _upgrade(isolated_database, "0029")
    assert _revision(isolated_database) == "0028"

    engine = sa.create_engine(isolated_database)
    try:
        with engine.connect() as connection:
            assert _fingerprint(connection) == (
                HEAD_EXECUTION_CHECK_RENDERING_DRIFT_FINGERPRINT
            )
    finally:
        engine.dispose()


def test_non_postgresql_does_not_gain_destructive_repair() -> None:
    engine = sa.create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            assert reconcile_head_execution_check_rendering(
                connection,
                "0029",
            ) is False
    finally:
        engine.dispose()
