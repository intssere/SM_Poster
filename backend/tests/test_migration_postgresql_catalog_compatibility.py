"""Real PostgreSQL clean-chain and fail-closed catalog compatibility."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest
import sqlalchemy as sa

from app.db import migration_adoption as adoption
from tests.test_migration_schema_adoption import (
    BACKEND,
    POSTGRES_URL,
    _alembic,
    _current,
    _failed_alembic,
    isolated_database,
)

pytestmark = pytest.mark.skipif(
    not POSTGRES_URL, reason="Disposable PostgreSQL is required"
)


def _verify_head(url: str) -> None:
    engine = sa.create_engine(url)
    try:
        with engine.connect() as connection:
            adoption.verify_frozen_schema_at_head(connection, revision="0034")
    finally:
        engine.dispose()


def test_empty_database_cli_upgrade_and_schema_canonicality(isolated_database):
    output = _alembic(isolated_database, "head")
    assert "Running upgrade 0033 -> 0034" in output
    assert _current(isolated_database) == "0034 (head)"
    _verify_head(isolated_database)


@pytest.mark.parametrize("revision", ["0031", "0032", "0033", "0034"])
def test_each_readiness_revision_keeps_frozen_contracts(isolated_database, revision):
    _alembic(isolated_database, revision)
    engine = sa.create_engine(isolated_database)
    try:
        with engine.connect() as connection:
            adoption.verify_frozen_schema_at_head(connection, revision=revision)
    finally:
        engine.dispose()


def test_canonical_existing_0023_retains_predecessors(isolated_database):
    _alembic(isolated_database, "0023")
    engine = sa.create_engine(isolated_database)
    try:
        with engine.connect() as connection:
            assert adoption._table_fingerprints(
                connection, adoption.DEV_0023_PRESERVED_TABLES
            ) == {
                table: adoption.FROZEN_FINGERPRINTS[table]
                for table in adoption.DEV_0023_PRESERVED_TABLES
            }
            before = connection.execute(sa.text(
                "SELECT relname, oid FROM pg_class "
                "WHERE relnamespace='public'::regnamespace "
                "AND relname=ANY(:tables) ORDER BY relname"
            ), {"tables": list(adoption.DEV_0023_PRESERVED_TABLES)}).all()
        _alembic(isolated_database, "head")
        with engine.connect() as connection:
            after = connection.execute(sa.text(
                "SELECT relname, oid FROM pg_class "
                "WHERE relnamespace='public'::regnamespace "
                "AND relname=ANY(:tables) ORDER BY relname"
            ), {"tables": list(adoption.DEV_0023_PRESERVED_TABLES)}).all()
        assert after == before
    finally:
        engine.dispose()
    _verify_head(isolated_database)


@pytest.mark.parametrize("table", adoption.DEV_0023_PRESERVED_TABLES)
def test_noncanonical_existing_0023_predecessor_refuses(isolated_database, table):
    _alembic(isolated_database, "0023")
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(f'ALTER TABLE "{table}" ADD COLUMN tampered integer'))
        output = _failed_alembic(isolated_database, "head")
        assert "canonical predecessor contracts do not match frozen fingerprints" in output
        with engine.connect() as connection:
            assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0023"
            assert connection.scalar(sa.text("SELECT to_regclass('public.pinterest_learning_snapshots')")) is None
    finally:
        engine.dispose()


def test_partial_predecessor_refuses_without_bookkeeping_change(isolated_database):
    _alembic(isolated_database, "0023")
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            connection.execute(sa.text('ALTER TABLE pinterest_seo_briefs RENAME TO missing_predecessor'))
        assert "canonical predecessor table presence mismatch" in _failed_alembic(
            isolated_database, "head"
        )
        with engine.connect() as connection:
            assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0023"
    finally:
        engine.dispose()


@pytest.mark.parametrize("table", adoption.DEV_0023_PRESERVED_TABLES)
def test_tampered_frozen_fingerprint_is_not_bypassed(isolated_database, monkeypatch, table):
    _alembic(isolated_database, "0023")
    monkeypatch.setitem(adoption.FROZEN_FINGERPRINTS, table, "0" * 64)
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            with pytest.raises(adoption.SchemaAdoptionRefused, match="canonical predecessor contracts"):
                adoption.reconcile_development_drift_at_0024(connection, "0024")
    finally:
        engine.dispose()


def test_supported_empty_head_downgrade_and_reupgrade(isolated_database):
    _alembic(isolated_database, "head")
    subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0031"],
        cwd=BACKEND,
        env={**os.environ, "DATABASE_URL": isolated_database, "PYTHONPATH": str(BACKEND)},
        check=True, capture_output=True, text=True,
    )
    _alembic(isolated_database, "head")
    _verify_head(isolated_database)


def test_dropped_not_null_is_not_normalized_away(isolated_database):
    _alembic(isolated_database, "0023")
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(
                "ALTER TABLE pinterest_seo_briefs ALTER COLUMN primary_keyword DROP NOT NULL"
            ))
        assert "canonical predecessor contracts" in _failed_alembic(isolated_database, "head")
    finally:
        engine.dispose()


@pytest.fixture
def pg18_database(isolated_database):
    engine = sa.create_engine(isolated_database)
    try:
        with engine.connect():
            if engine.dialect.server_version_info < (18,):
                pytest.skip("PostgreSQL 18-only constraint syntax")
    finally:
        engine.dispose()
    _alembic(isolated_database, "0023")
    return isolated_database


@pytest.mark.parametrize(
    "definition",
    [
        "ADD CONSTRAINT unsafe_nn NOT NULL primary_keyword NOT VALID",
        "ADD CONSTRAINT unsafe_nn NOT NULL primary_keyword NO INHERIT",
    ],
)
def test_pg18_weaker_not_null_refuses(pg18_database, definition):
    engine = sa.create_engine(pg18_database)
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(
                "ALTER TABLE pinterest_seo_briefs ALTER COLUMN primary_keyword DROP NOT NULL"
            ))
            connection.execute(sa.text(f"ALTER TABLE pinterest_seo_briefs {definition}"))
        assert "noncanonical NOT NULL constraint" in _failed_alembic(pg18_database, "head")
        with engine.connect() as connection:
            assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0023"
    finally:
        engine.dispose()


def test_pg18_unenforced_check_refuses(pg18_database):
    engine = sa.create_engine(pg18_database)
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(
                "ALTER TABLE pinterest_seo_briefs DROP CONSTRAINT ck_pinterest_seo_brief_status"
            ))
            connection.execute(sa.text(
                "ALTER TABLE pinterest_seo_briefs ADD CONSTRAINT ck_pinterest_seo_brief_status "
                "CHECK (status IN ('CURRENT','SUPERSEDED')) NOT ENFORCED"
            ))
        assert "unenforced constraint" in _failed_alembic(pg18_database, "head")
        with engine.connect() as connection:
            assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0023"
    finally:
        engine.dispose()