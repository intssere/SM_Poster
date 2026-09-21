"""Task #58.5 exact development-schema reconciliation at Alembic 0023."""
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
    BUNDLE_TABLES,
    DEV_0023_DRIFT_FINGERPRINTS,
    DEV_0023_DRIFT_TABLES,
    DEV_0023_PRESERVED_TABLES,
    FROZEN_FINGERPRINTS,
    SchemaAdoptionRefused,
    _catalog_contract,
)


POSTGRES_URL = os.getenv("TASK58_POSTGRES_URL")
BACKEND = Path(__file__).resolve().parents[1]
REAL_FINGERPRINT = migration_adoption._fingerprint


def _admin_url(url: str) -> str:
    return make_url(url).set(database="postgres").render_as_string(
        hide_password=False
    )


@pytest.fixture
def isolated_database() -> Iterator[str]:
    if not POSTGRES_URL:
        pytest.skip("TASK58_POSTGRES_URL is required for Task #58.5 tests")
    name = f"task585_{uuid4().hex[:16]}"
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


def _set_revision_0023(url: str) -> None:
    engine = sa.create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE alembic_version SET version_num='0023'")
            )
    finally:
        engine.dispose()


def _fingerprints(connection, tables) -> dict[str, str]:
    return {
        table: REAL_FINGERPRINT(_catalog_contract(connection, table))
        for table in tables
    }


def _oids(connection, tables) -> dict[str, int]:
    rows = connection.execute(
        sa.text(
            """SELECT relname, oid
            FROM pg_class
            WHERE relnamespace='public'::regnamespace
              AND relname = ANY(:tables)
            ORDER BY relname"""
        ),
        {"tables": list(tables)},
    ).all()
    return {str(name): int(oid) for name, oid in rows}


_PROXY_RENAMES = {
    "pinterest_analytics_snapshots": (
        "ix_pinterest_analytics_snapshots_payload_fp",
        "task585_analytics_payload",
    ),
    "pinterest_analytics_ingestion_runs": (
        "ix_pinterest_analytics_runs_payload_fp",
        "task585_ingestion_payload",
    ),
    "pinterest_learning_snapshots": (
        "ix_pinterest_learning_snapshots_input_fp",
        "task585_learning_input",
    ),
    "pinterest_optimizer_applications": (
        "ix_pinterest_optimizer_applications_input_state",
        "task585_optimizer_input",
    ),
    "pinterest_autonomous_execution_runs": (
        "ix_pinterest_auto_exec_status",
        "task585_exec_status",
    ),
    "pinterest_autonomous_destination_runs": (
        "ix_pinterest_auto_destination_status",
        "task585_destination_status",
    ),
}


def _prepare_preapplied_future_bundle(url: str) -> dict[str, str]:
    _upgrade(url, "0027")
    _set_revision_0023(url)
    engine = sa.create_engine(url)
    try:
        with engine.begin() as connection:
            for old, new in _PROXY_RENAMES.values():
                connection.execute(
                    sa.text(
                        f'ALTER INDEX "public"."{old}" RENAME TO "{new}"'
                    )
                )
            return _fingerprints(connection, DEV_0023_DRIFT_TABLES)
    finally:
        engine.dispose()


def _alias_proxy_to_certified_dev(
    proxy: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    overrides: dict[str, str] | None = None,
) -> None:
    expected = dict(DEV_0023_DRIFT_FINGERPRINTS)
    if overrides:
        expected.update(overrides)
    aliases = {
        proxy[table]: expected[table]
        for table in DEV_0023_DRIFT_TABLES
    }

    def aliased(contract):
        real = REAL_FINGERPRINT(contract)
        return aliases.get(real, real)

    monkeypatch.setattr(migration_adoption, "_fingerprint", aliased)


def _insert_generic_row(connection, table: str) -> None:
    columns = connection.execute(
        sa.text(
            """SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name=:table
            ORDER BY ordinal_position"""
        ),
        {"table": table},
    ).all()
    statuses = {
        "pinterest_analytics_ingestion_runs": "STARTED",
        "pinterest_optimizer_applications": "APPLIED",
        "pinterest_autonomous_execution_runs": "STARTED",
        "pinterest_autonomous_destination_runs": "STARTED",
    }
    names: list[str] = []
    values: list[str] = []
    for column, data_type, nullable, default in columns:
        if default is not None:
            continue
        names.append(f'"{column}"')
        if column == "status" and table in statuses:
            values.append(f"'{statuses[table]}'")
        elif column == "stage":
            values.append("'STARTED'")
        elif column == "observation_window":
            values.append("'D1'")
        elif str(nullable) == "YES":
            values.append("NULL")
        elif data_type in {"character varying", "text"}:
            values.append("'x'")
        elif data_type == "date":
            values.append("CURRENT_DATE")
        elif str(data_type).startswith("timestamp"):
            values.append("CURRENT_TIMESTAMP")
        elif data_type in {"integer", "numeric", "bigint"}:
            values.append("0")
        elif data_type == "boolean":
            values.append("false")
        elif data_type in {"json", "jsonb"}:
            values.append("'{}'::json")
        else:
            raise AssertionError(
                f"no generic value for {table}.{column} ({data_type})"
            )

    connection.execute(sa.text("SET LOCAL session_replication_role = replica"))
    connection.execute(
        sa.text(
            f'INSERT INTO "public"."{table}" ({", ".join(names)}) '
            f'VALUES ({", ".join(values)})'
        )
    )


def test_exact_development_allowlist_is_frozen() -> None:
    assert DEV_0023_DRIFT_FINGERPRINTS == {
        "pinterest_analytics_snapshots":
            "54751d9124942ca617d34da53e997f45f2a5924efae2d92deadfca944b5ca25e",
        "pinterest_analytics_ingestion_runs":
            "4aaccd7247ba75c43f3319842b3a86a816b55cb205537cb7d1dfb1da2abba3c5",
        "pinterest_learning_snapshots":
            "985935d8d8424d69bf5003e6c0c0e55656eb8990b418ec0cd3c23e4219f6e231",
        "pinterest_optimizer_applications":
            "2605cb38c7f05c91f92931ab836592051277048fb711ce6cc8a03d8347fc7889",
        "pinterest_autonomous_execution_runs":
            "26cb4cbfa1b2adb97f42a0ef8f8a0700c40937b06e07387ab57e9cffffc8d28d",
        "pinterest_autonomous_destination_runs":
            "fc7f6af3194be33a88ae130358250d75c08e0fd839606dcb1f86ef54fce7664b",
    }


def test_exact_dev_0023_state_upgrades_to_canonical_head(
    isolated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy = _prepare_preapplied_future_bundle(isolated_database)
    _alias_proxy_to_certified_dev(proxy, monkeypatch)

    engine = sa.create_engine(isolated_database)
    try:
        with engine.connect() as connection:
            predecessor_oids = _oids(
                connection,
                DEV_0023_PRESERVED_TABLES,
            )
    finally:
        engine.dispose()

    _upgrade(isolated_database, "head")
    assert _revision(isolated_database) == "0028"

    engine = sa.create_engine(isolated_database)
    try:
        with engine.connect() as connection:
            assert _oids(
                connection,
                DEV_0023_PRESERVED_TABLES,
            ) == predecessor_oids
            actual = _fingerprints(connection, BUNDLE_TABLES)
    finally:
        engine.dispose()
    assert actual == {
        table: FROZEN_FINGERPRINTS[table]
        for table in BUNDLE_TABLES
    }


def test_canonical_0023_without_future_tables_upgrades_normally(
    isolated_database: str,
) -> None:
    _upgrade(isolated_database, "0023")
    _upgrade(isolated_database, "head")
    assert _revision(isolated_database) == "0028"


def test_unknown_dev_fingerprint_refuses_at_0024(
    isolated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy = _prepare_preapplied_future_bundle(isolated_database)
    _alias_proxy_to_certified_dev(
        proxy,
        monkeypatch,
        overrides={"pinterest_autonomous_execution_runs": "0" * 64},
    )
    with pytest.raises(
        SchemaAdoptionRefused,
        match="not the exact Task #58.5 fingerprint set",
    ):
        _upgrade(isolated_database, "head")
    assert _revision(isolated_database) == "0023"


def test_missing_target_refuses_at_0024(isolated_database: str) -> None:
    _upgrade(isolated_database, "0027")
    _set_revision_0023(isolated_database)
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(
                'DROP TABLE "public"."pinterest_autonomous_destination_runs"'
            ))
    finally:
        engine.dispose()

    with pytest.raises(
        SchemaAdoptionRefused,
        match="partial table presence",
    ):
        _upgrade(isolated_database, "head")
    assert _revision(isolated_database) == "0023"


@pytest.mark.parametrize("table", DEV_0023_DRIFT_TABLES)
def test_each_nonempty_target_refuses(
    isolated_database: str,
    monkeypatch: pytest.MonkeyPatch,
    table: str,
) -> None:
    proxy = _prepare_preapplied_future_bundle(isolated_database)
    _alias_proxy_to_certified_dev(proxy, monkeypatch)
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            _insert_generic_row(connection, table)
    finally:
        engine.dispose()

    with pytest.raises(SchemaAdoptionRefused, match="is not empty"):
        _upgrade(isolated_database, "head")
    assert _revision(isolated_database) == "0023"


def test_noncanonical_predecessor_refuses(
    isolated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy = _prepare_preapplied_future_bundle(isolated_database)
    _alias_proxy_to_certified_dev(proxy, monkeypatch)
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(
                'ALTER TABLE "public"."pinterest_seo_briefs" '
                'ADD COLUMN task585_predecessor_drift integer'
            ))
    finally:
        engine.dispose()

    with pytest.raises(
        SchemaAdoptionRefused,
        match="canonical predecessor contracts",
    ):
        _upgrade(isolated_database, "head")
    assert _revision(isolated_database) == "0023"


def test_nonempty_predecessor_refuses(
    isolated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy = _prepare_preapplied_future_bundle(isolated_database)
    _alias_proxy_to_certified_dev(proxy, monkeypatch)
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            _insert_generic_row(connection, "pinterest_seo_briefs")
    finally:
        engine.dispose()

    with pytest.raises(SchemaAdoptionRefused, match="is not empty"):
        _upgrade(isolated_database, "head")
    assert _revision(isolated_database) == "0023"


def test_external_dependency_refuses(
    isolated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy = _prepare_preapplied_future_bundle(isolated_database)
    _alias_proxy_to_certified_dev(proxy, monkeypatch)
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(
                """CREATE TABLE task585_external_fk (
                    id integer PRIMARY KEY,
                    snapshot_id varchar(36),
                    CONSTRAINT fk_task585_snapshot
                    FOREIGN KEY (snapshot_id)
                    REFERENCES pinterest_analytics_snapshots(id)
                )"""
            ))
    finally:
        engine.dispose()

    with pytest.raises(
        SchemaAdoptionRefused,
        match="unexpected external dependencies",
    ):
        _upgrade(isolated_database, "head")
    assert _revision(isolated_database) == "0023"


def test_lock_recheck_refuses_fingerprint_change(
    isolated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _upgrade(isolated_database, "0027")
    _set_revision_0023(isolated_database)
    real_table_fingerprints = migration_adoption._table_fingerprints
    target_calls = 0

    def changing(connection, tables):
        nonlocal target_calls
        if tuple(tables) != DEV_0023_DRIFT_TABLES:
            return real_table_fingerprints(connection, tables)
        target_calls += 1
        if target_calls == 1:
            return dict(DEV_0023_DRIFT_FINGERPRINTS)
        changed = dict(DEV_0023_DRIFT_FINGERPRINTS)
        changed["pinterest_analytics_snapshots"] = "f" * 64
        return changed

    monkeypatch.setattr(
        migration_adoption,
        "_table_fingerprints",
        changing,
    )

    with pytest.raises(
        SchemaAdoptionRefused,
        match="fingerprints changed while locking",
    ):
        _upgrade(isolated_database, "head")
    assert _revision(isolated_database) == "0023"


def test_unrecognized_drop_dependency_rolls_back_without_cascade(
    isolated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy = _prepare_preapplied_future_bundle(isolated_database)
    _alias_proxy_to_certified_dev(proxy, monkeypatch)
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(
                """CREATE TABLE task585_hidden_dependency (
                    id integer PRIMARY KEY,
                    snapshot_id varchar(36),
                    CONSTRAINT fk_task585_hidden
                    FOREIGN KEY (snapshot_id)
                    REFERENCES pinterest_analytics_snapshots(id)
                )"""
            ))
    finally:
        engine.dispose()

    monkeypatch.setattr(
        migration_adoption,
        "_external_dependencies_for_tables",
        lambda connection, tables: [],
    )
    with pytest.raises(sa.exc.DBAPIError):
        _upgrade(isolated_database, "head")

    assert _revision(isolated_database) == "0023"
    engine = sa.create_engine(isolated_database)
    try:
        with engine.connect() as connection:
            assert connection.execute(
                sa.text(
                    "SELECT to_regclass('public.task585_hidden_dependency')"
                )
            ).scalar_one() is not None
            assert _fingerprints(
                connection,
                DEV_0023_DRIFT_TABLES,
            ) == proxy
    finally:
        engine.dispose()


def test_post_analytics_rebuild_mismatch_rolls_back(
    isolated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy = _prepare_preapplied_future_bundle(isolated_database)
    _alias_proxy_to_certified_dev(proxy, monkeypatch)
    monkeypatch.setitem(
        FROZEN_FINGERPRINTS,
        "pinterest_analytics_snapshots",
        "1" * 64,
    )

    with pytest.raises(
        SchemaAdoptionRefused,
        match="canonical predecessor contracts",
    ):
        _upgrade(isolated_database, "head")

    assert _revision(isolated_database) == "0023"
    engine = sa.create_engine(isolated_database)
    try:
        with engine.connect() as connection:
            assert _fingerprints(
                connection,
                DEV_0023_DRIFT_TABLES,
            ) == proxy
    finally:
        engine.dispose()
