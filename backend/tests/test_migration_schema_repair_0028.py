"""Task #58.3 exact six-table post-publish drift repair."""
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
    POST_PUBLISH_DRIFT_DROP_ORDER,
    POST_PUBLISH_DRIFT_FINGERPRINTS,
    POST_PUBLISH_DRIFT_TABLES,
    POST_PUBLISH_PRESERVED_TABLES,
    SchemaAdoptionRefused,
    _catalog_contract,
    reconcile_post_publish_drift,
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
        pytest.skip("TASK58_POSTGRES_URL is required for Task #58.3 tests")
    name = f"task583_{uuid4().hex[:16]}"
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


def _real_fingerprints(connection, tables) -> dict[str, str]:
    return {
        table: REAL_FINGERPRINT(_catalog_contract(connection, table))
        for table in tables
    }


def _oids(connection, tables) -> dict[str, int]:
    rows = connection.execute(
        sa.text(
            """SELECT relname, oid
            FROM pg_class
            WHERE relnamespace = 'public'::regnamespace
              AND relname = ANY(:tables)
            ORDER BY relname"""
        ),
        {"tables": list(tables)},
    ).all()
    return {str(name): int(oid) for name, oid in rows}


_PROXY_RENAMES = {
    "pinterest_analytics_snapshots":
        ("ix_pinterest_analytics_snapshots_payload_fp", "proxy_583_analytics_payload"),
    "pinterest_analytics_ingestion_runs":
        ("ix_pinterest_analytics_runs_payload_fp", "proxy_583_ingestion_payload"),
    "pinterest_learning_snapshots":
        ("ix_pinterest_learning_snapshots_input_fp", "proxy_583_learning_input"),
    "pinterest_optimizer_applications":
        ("ix_pinterest_optimizer_applications_input_state", "proxy_583_optimizer_input"),
    "pinterest_autonomous_execution_runs":
        ("ix_pinterest_auto_exec_status", "proxy_583_exec_status"),
    "pinterest_autonomous_destination_runs":
        ("ix_pinterest_auto_destination_status", "proxy_583_destination_status"),
}


def _install_proxy_drift(url: str) -> dict[str, str]:
    engine = sa.create_engine(url)
    try:
        with engine.begin() as connection:
            for old, new in _PROXY_RENAMES.values():
                connection.execute(
                    sa.text(f'ALTER INDEX "public"."{old}" RENAME TO "{new}"')
                )
            return _real_fingerprints(connection, POST_PUBLISH_DRIFT_TABLES)
    finally:
        engine.dispose()


def _alias_proxy_to_certified_drift(
    proxy: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aliases = {
        proxy[table]: POST_PUBLISH_DRIFT_FINGERPRINTS[table]
        for table in POST_PUBLISH_DRIFT_TABLES
    }

    def aliased_fingerprint(contract):
        real = REAL_FINGERPRINT(contract)
        return aliases.get(real, real)

    monkeypatch.setattr(migration_adoption, "_fingerprint", aliased_fingerprint)


def _insert_generic_row(connection, table: str) -> None:
    columns = connection.execute(
        sa.text(
            """SELECT column_name, data_type, is_nullable
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
    for column, data_type, nullable in columns:
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


def test_only_certified_post_publish_fingerprints_are_allowlisted() -> None:
    assert POST_PUBLISH_DRIFT_FINGERPRINTS == {
        "pinterest_analytics_snapshots":
            "54751d9124942ca617d34da53e997f45f2a5924efae2d92deadfca944b5ca25e",
        "pinterest_analytics_ingestion_runs":
            "4aaccd7247ba75c43f3319842b3a86a816b55cb205537cb7d1dfb1da2abba3c5",
        "pinterest_learning_snapshots":
            "985935d8d8424d69bf5003e6c0c0e55656eb8990b418ec0cd3c23e4219f6e231",
        "pinterest_optimizer_applications":
            "2605cb38c7f05c91f92931ab836592051277048fb711ce6cc8a03d8347fc7889",
        "pinterest_autonomous_execution_runs":
            "92143aab35020f091c1ae917d7c6aed7ebe7880302abffccda79d8c4f5416af6",
        "pinterest_autonomous_destination_runs":
            "fc7f6af3194be33a88ae130358250d75c08e0fd839606dcb1f86ef54fce7664b",
    }
    assert POST_PUBLISH_DRIFT_DROP_ORDER == (
        "pinterest_autonomous_destination_runs",
        "pinterest_autonomous_execution_runs",
        "pinterest_optimizer_applications",
        "pinterest_analytics_ingestion_runs",
        "pinterest_analytics_snapshots",
        "pinterest_learning_snapshots",
    )


def test_fresh_upgrade_reaches_current_head_and_is_canonical(
    isolated_database: str,
) -> None:
    _upgrade(isolated_database, "0029")
    assert _revision(isolated_database) == "0029"
    engine = sa.create_engine(isolated_database)
    try:
        with engine.connect() as connection:
            actual = _real_fingerprints(
                connection,
                POST_PUBLISH_PRESERVED_TABLES + POST_PUBLISH_DRIFT_TABLES,
            )
    finally:
        engine.dispose()
    assert actual == {
        table: FROZEN_FINGERPRINTS[table]
        for table in POST_PUBLISH_PRESERVED_TABLES + POST_PUBLISH_DRIFT_TABLES
    }


def test_canonical_0027_to_0028_is_no_rebuild(
    isolated_database: str,
) -> None:
    _upgrade(isolated_database, "0027")
    engine = sa.create_engine(isolated_database)
    try:
        with engine.connect() as connection:
            before = _oids(connection, POST_PUBLISH_DRIFT_TABLES)
    finally:
        engine.dispose()

    _upgrade(isolated_database, "0028")
    assert _revision(isolated_database) == "0028"

    engine = sa.create_engine(isolated_database)
    try:
        with engine.connect() as connection:
            after = _oids(connection, POST_PUBLISH_DRIFT_TABLES)
            fingerprints = _real_fingerprints(
                connection,
                POST_PUBLISH_DRIFT_TABLES,
            )
    finally:
        engine.dispose()
    assert after == before
    assert fingerprints == {
        table: FROZEN_FINGERPRINTS[table]
        for table in POST_PUBLISH_DRIFT_TABLES
    }


def test_exact_certified_drift_repairs_only_six_tables(
    isolated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _upgrade(isolated_database, "0027")
    engine = sa.create_engine(isolated_database)
    try:
        with engine.connect() as connection:
            predecessor_oids = _oids(connection, POST_PUBLISH_PRESERVED_TABLES)
            predecessor_fingerprints = _real_fingerprints(
                connection,
                POST_PUBLISH_PRESERVED_TABLES,
            )
    finally:
        engine.dispose()

    proxy = _install_proxy_drift(isolated_database)
    _alias_proxy_to_certified_drift(proxy, monkeypatch)
    _upgrade(isolated_database, "0028")

    assert _revision(isolated_database) == "0028"
    engine = sa.create_engine(isolated_database)
    try:
        with engine.connect() as connection:
            assert _oids(connection, POST_PUBLISH_PRESERVED_TABLES) == predecessor_oids
            assert _real_fingerprints(
                connection,
                POST_PUBLISH_PRESERVED_TABLES,
            ) == predecessor_fingerprints
            assert _real_fingerprints(
                connection,
                POST_PUBLISH_DRIFT_TABLES,
            ) == {
                table: FROZEN_FINGERPRINTS[table]
                for table in POST_PUBLISH_DRIFT_TABLES
            }
    finally:
        engine.dispose()


@pytest.mark.parametrize("table", POST_PUBLISH_DRIFT_TABLES)
def test_every_nonempty_target_refuses(
    isolated_database: str,
    monkeypatch: pytest.MonkeyPatch,
    table: str,
) -> None:
    _upgrade(isolated_database, "0027")
    proxy = _install_proxy_drift(isolated_database)
    _alias_proxy_to_certified_drift(proxy, monkeypatch)

    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            _insert_generic_row(connection, table)
    finally:
        engine.dispose()

    with pytest.raises(SchemaAdoptionRefused, match="is not empty"):
        _upgrade(isolated_database, "0028")
    assert _revision(isolated_database) == "0027"


def test_partial_target_presence_refuses(isolated_database: str) -> None:
    _upgrade(isolated_database, "0027")
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    'DROP TABLE "public"."pinterest_autonomous_destination_runs"'
                )
            )
    finally:
        engine.dispose()

    with pytest.raises(SchemaAdoptionRefused, match="partial table presence"):
        _upgrade(isolated_database, "0028")
    assert _revision(isolated_database) == "0027"


def test_one_unknown_fingerprint_refuses(
    isolated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _upgrade(isolated_database, "0027")
    proxy = _install_proxy_drift(isolated_database)
    aliases = {
        proxy[table]: POST_PUBLISH_DRIFT_FINGERPRINTS[table]
        for table in POST_PUBLISH_DRIFT_TABLES
    }
    aliases[proxy["pinterest_learning_snapshots"]] = "0" * 64

    def aliased(contract):
        real = REAL_FINGERPRINT(contract)
        return aliases.get(real, real)

    monkeypatch.setattr(migration_adoption, "_fingerprint", aliased)
    with pytest.raises(SchemaAdoptionRefused, match="not the exact Task #58.3"):
        _upgrade(isolated_database, "0028")
    assert _revision(isolated_database) == "0027"


def test_mixed_canonical_and_drift_refuses(
    isolated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _upgrade(isolated_database, "0027")
    proxy = _install_proxy_drift(isolated_database)
    aliases = {
        proxy[table]: POST_PUBLISH_DRIFT_FINGERPRINTS[table]
        for table in POST_PUBLISH_DRIFT_TABLES
    }
    aliases.pop(proxy["pinterest_autonomous_destination_runs"])

    def aliased(contract):
        real = REAL_FINGERPRINT(contract)
        return aliases.get(real, real)

    monkeypatch.setattr(migration_adoption, "_fingerprint", aliased)
    with pytest.raises(SchemaAdoptionRefused, match="not the exact Task #58.3"):
        _upgrade(isolated_database, "0028")
    assert _revision(isolated_database) == "0027"


def test_external_foreign_key_refuses(
    isolated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _upgrade(isolated_database, "0027")
    proxy = _install_proxy_drift(isolated_database)
    _alias_proxy_to_certified_drift(proxy, monkeypatch)
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(
                """CREATE TABLE task583_external_fk (
                    id integer PRIMARY KEY,
                    snapshot_id varchar(36),
                    CONSTRAINT fk_task583_snapshot
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
        _upgrade(isolated_database, "0028")
    assert _revision(isolated_database) == "0027"


def test_lock_recheck_refuses_concurrent_fingerprint_drift(
    isolated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _upgrade(isolated_database, "0027")
    real_table_fingerprints = migration_adoption._table_fingerprints
    target_calls = 0

    def changing(connection, tables):
        nonlocal target_calls
        if tuple(tables) != POST_PUBLISH_DRIFT_TABLES:
            return real_table_fingerprints(connection, tables)
        target_calls += 1
        if target_calls == 1:
            return dict(POST_PUBLISH_DRIFT_FINGERPRINTS)
        changed = dict(POST_PUBLISH_DRIFT_FINGERPRINTS)
        changed["pinterest_analytics_snapshots"] = "f" * 64
        return changed

    monkeypatch.setattr(migration_adoption, "_table_fingerprints", changing)
    with pytest.raises(
        SchemaAdoptionRefused,
        match="fingerprints changed while locking",
    ):
        _upgrade(isolated_database, "0028")
    assert _revision(isolated_database) == "0027"


def test_unrecognized_drop_dependency_rolls_back_without_cascade(
    isolated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _upgrade(isolated_database, "0027")
    proxy = _install_proxy_drift(isolated_database)
    _alias_proxy_to_certified_drift(proxy, monkeypatch)
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(
                """CREATE TABLE task583_unrecognized_dependency (
                    id integer PRIMARY KEY,
                    snapshot_id varchar(36),
                    CONSTRAINT fk_task583_unrecognized
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
        _upgrade(isolated_database, "0028")

    assert _revision(isolated_database) == "0027"
    engine = sa.create_engine(isolated_database)
    try:
        with engine.connect() as connection:
            assert connection.execute(
                sa.text("SELECT to_regclass('public.task583_unrecognized_dependency')")
            ).scalar_one() is not None
            assert _real_fingerprints(
                connection,
                POST_PUBLISH_DRIFT_TABLES,
            ) == proxy
    finally:
        engine.dispose()


def test_post_rebuild_mismatch_rolls_back_entire_repair(
    isolated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _upgrade(isolated_database, "0027")
    proxy = _install_proxy_drift(isolated_database)
    _alias_proxy_to_certified_drift(proxy, monkeypatch)
    monkeypatch.setitem(
        FROZEN_FINGERPRINTS,
        "pinterest_autonomous_destination_runs",
        "1" * 64,
    )

    with pytest.raises(
        SchemaAdoptionRefused,
        match="canonical predecessor contracts|fingerprint",
    ):
        _upgrade(isolated_database, "0028")

    assert _revision(isolated_database) == "0027"
    engine = sa.create_engine(isolated_database)
    try:
        with engine.connect() as connection:
            assert _real_fingerprints(
                connection,
                POST_PUBLISH_DRIFT_TABLES,
            ) == proxy
    finally:
        engine.dispose()


def test_startup_guard_accepts_canonical_head_and_refuses_drift_read_only(
    isolated_database: str,
) -> None:
    _upgrade(isolated_database, "head")
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            verify_frozen_schema_at_head(connection)
            connection.execute(sa.text(
                'ALTER INDEX "public"."ix_pinterest_auto_destination_status" '
                'RENAME TO "task583_guard_drift"'
            ))
        with engine.connect() as connection:
            with pytest.raises(
                SchemaAdoptionRefused,
                match="index contract mismatch",
            ):
                verify_frozen_schema_at_head(connection)
            assert connection.execute(
                sa.text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "0030"
            assert connection.execute(
                sa.text(
                    "SELECT to_regclass("
                    "'public.task583_guard_drift')"
                )
            ).scalar_one() is not None
    finally:
        engine.dispose()


def test_non_postgresql_does_not_gain_destructive_head_repair() -> None:
    engine = sa.create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            assert reconcile_post_publish_drift(connection, "0028") is False
    finally:
        engine.dispose()
