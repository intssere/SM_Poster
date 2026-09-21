"""Issue #117 exact pre-applied 0020-0027 bundle reconciliation."""
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
from app.db.base import Base
from app.db.migration_adoption import (
    BUNDLE_TABLES,
    FROZEN_FINGERPRINTS,
    PREAPPLIED_BUNDLE_DROP_ORDER,
    PREAPPLIED_BUNDLE_FINGERPRINTS,
    SchemaAdoptionRefused,
    _catalog_contract,
    _fingerprint,
    reconcile_preapplied_bundle,
)
from app.models import domain  # noqa: F401


POSTGRES_URL = os.getenv("TASK58_CATALOG_URL") or os.getenv("TASK58_POSTGRES_URL")
BACKEND = Path(__file__).resolve().parents[1]
REAL_FINGERPRINT = _fingerprint


def _admin_url(url: str) -> str:
    parsed = make_url(url)
    return parsed.set(database="postgres").render_as_string(hide_password=False)


@pytest.fixture
def isolated_database() -> Iterator[str]:
    if not POSTGRES_URL:
        pytest.skip("TASK58_POSTGRES_URL is required for bundle tests")
    name = f"task582_{uuid4().hex[:16]}"
    admin = sa.create_engine(_admin_url(POSTGRES_URL), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
        yield make_url(POSTGRES_URL).set(
            database=name
        ).render_as_string(hide_password=False)
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
    old_database_url = os.environ.get("DATABASE_URL")
    old_cwd = Path.cwd()
    os.environ["DATABASE_URL"] = url
    get_settings.cache_clear()
    os.chdir(BACKEND)
    try:
        yield
    finally:
        os.chdir(old_cwd)
        if old_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old_database_url
        get_settings.cache_clear()


def _upgrade(url: str, target: str) -> None:
    with _database_environment(url):
        command.upgrade(Config(str(BACKEND / "alembic.ini")), target)


def _revision(url: str) -> str:
    engine = sa.create_engine(url)
    try:
        with engine.connect() as connection:
            return str(connection.execute(
                sa.text("SELECT version_num FROM alembic_version")
            ).scalar_one())
    finally:
        engine.dispose()


def _set_revision(url: str, revision: str) -> None:
    engine = sa.create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE alembic_version SET version_num=:revision"),
                {"revision": revision},
            )
    finally:
        engine.dispose()


def _fingerprints(connection, tables=BUNDLE_TABLES) -> dict[str, str]:
    return {
        table: REAL_FINGERPRINT(_catalog_contract(connection, table))
        for table in tables
    }


def _create_proxy_bundle(url: str) -> dict[str, str]:
    engine = sa.create_engine(url)
    try:
        with engine.begin() as connection:
            Base.metadata.create_all(
                connection,
                tables=[Base.metadata.tables[table] for table in BUNDLE_TABLES],
            )
            # Task #58.3 intentionally makes the six later ORM models exactly
            # canonical. This older Task #58.2 fixture still needs a disposable
            # noncanonical catalog so aliasing it to the certified Issue #117
            # fingerprints cannot also alias freshly rebuilt canonical tables.
            for old_name, new_name in (
                (
                    "ix_pinterest_analytics_snapshots_payload_fp",
                    "proxy_582_analytics_payload",
                ),
                (
                    "ix_pinterest_analytics_runs_payload_fp",
                    "proxy_582_ingestion_payload",
                ),
                (
                    "ix_pinterest_learning_snapshots_input_fp",
                    "proxy_582_learning_input",
                ),
                (
                    "ix_pinterest_optimizer_applications_input_state",
                    "proxy_582_optimizer_input",
                ),
                (
                    "ix_pinterest_auto_exec_status",
                    "proxy_582_exec_status",
                ),
                (
                    "ix_pinterest_auto_destination_status",
                    "proxy_582_destination_status",
                ),
            ):
                connection.execute(
                    sa.text(
                        f'ALTER INDEX "public"."{old_name}" '
                        f'RENAME TO "{new_name}"'
                    )
                )
            proxy = _fingerprints(connection)
            for table in BUNDLE_TABLES:
                assert proxy[table] != FROZEN_FINGERPRINTS[table]
            return proxy
    finally:
        engine.dispose()


def _alias_proxy_to_production_bundle(
    proxy: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alias only this disposable catalog to the certified production hashes."""
    aliases = {
        proxy[table]: PREAPPLIED_BUNDLE_FINGERPRINTS[table]
        for table in BUNDLE_TABLES
    }

    def aliased_fingerprint(contract):
        real = REAL_FINGERPRINT(contract)
        return aliases.get(real, real)

    monkeypatch.setattr(
        migration_adoption,
        "_fingerprint",
        aliased_fingerprint,
    )


def _insert_generic_row(connection, table: str) -> None:
    columns = connection.execute(sa.text(
        """SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=:table
        ORDER BY ordinal_position"""
    ), {"table": table}).all()
    status = {
        "pinterest_portfolio_plans": "DRAFT",
        "pinterest_portfolio_plan_items": "PLANNED",
        "pinterest_seo_briefs": "CURRENT",
        "pinterest_autonomous_generation_runs": "STARTED",
        "pinterest_analytics_ingestion_runs": "STARTED",
        "pinterest_optimizer_applications": "APPLIED",
        "pinterest_autonomous_execution_runs": "STARTED",
        "pinterest_autonomous_destination_runs": "STARTED",
    }.get(table)
    names: list[str] = []
    values: list[str] = []
    for column, data_type, nullable in columns:
        names.append(f'"{column}"')
        if column == "status" and status is not None:
            values.append(f"'{status}'")
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
    connection.execute(sa.text(
        f'INSERT INTO "public"."{table}" ({", ".join(names)}) '
        f'VALUES ({", ".join(values)})'
    ))


def _assert_proxy_bundle(url: str, expected: dict[str, str]) -> None:
    engine = sa.create_engine(url)
    try:
        with engine.begin() as connection:
            assert _fingerprints(connection) == expected
    finally:
        engine.dispose()


def test_only_certified_issue_117_bundle_is_allowlisted() -> None:
    assert PREAPPLIED_BUNDLE_FINGERPRINTS == {
        "pinterest_portfolio_plans":
            "9a91ae0ff2d722c7e12c23e236698cfd71259308e1ef3a42e4782075754129bf",
        "pinterest_portfolio_plan_items":
            "3085a01cf1385f9ed5cab605f236c7eceba49fab8f7ae4d6eac5e63e63f61274",
        "pinterest_seo_briefs":
            "32f3db20631cc243c492fb9fa9770a3fd33948b5b6345cc7bafa92275e7ee9e1",
        "pinterest_autonomous_generation_runs":
            "a6c0ccdb9c5f8d73e06b0c554976d5923b01ad62582cd42aa335aab5e2557a57",
        "pinterest_analytics_snapshots":
            "f48ddbaa2a3aa476e738a922be5bd894ec51f30c61bf452148c0a4eb4e09b720",
        "pinterest_analytics_ingestion_runs":
            "7863c20acda5feca706a72a96f047f9683031346613bef257ca9a99cfe1ffe39",
        "pinterest_learning_snapshots":
            "985935d8d8424d69bf5003e6c0c0e55656eb8990b418ec0cd3c23e4219f6e231",
        "pinterest_optimizer_applications":
            "2605cb38c7f05c91f92931ab836592051277048fb711ce6cc8a03d8347fc7889",
        "pinterest_autonomous_execution_runs":
            "e12a88dcc472ff7411ac09bf0f6d8ee89f4710f0e2a4e7f31e9689018c717c3a",
        "pinterest_autonomous_destination_runs":
            "9acb16898b1f5db70bfade1dabe29d449d26a49a5c8752693f30ca3b292d523f",
    }


def test_exact_bundle_reconciles_and_reaches_current_head(
    isolated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _upgrade(isolated_database, "0019")
    proxy = _create_proxy_bundle(isolated_database)
    _alias_proxy_to_production_bundle(proxy, monkeypatch)

    _upgrade(isolated_database, "head")

    assert _revision(isolated_database) == "0028"
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            assert _fingerprints(connection) == FROZEN_FINGERPRINTS
    finally:
        engine.dispose()


def test_fresh_upgrade_still_reaches_current_head(isolated_database: str) -> None:
    _upgrade(isolated_database, "head")
    assert _revision(isolated_database) == "0028"


def test_complete_canonical_bundle_still_adopts(isolated_database: str) -> None:
    _upgrade(isolated_database, "head")
    _set_revision(isolated_database, "0019")
    _upgrade(isolated_database, "head")
    assert _revision(isolated_database) == "0028"


def test_exact_0020_only_pair_is_left_for_task_58_1(
    isolated_database: str,
) -> None:
    _upgrade(isolated_database, "0020")
    _set_revision(isolated_database, "0019")
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            assert reconcile_preapplied_bundle(connection, "0020") is False
    finally:
        engine.dispose()


def test_one_fingerprint_drift_refuses(
    isolated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _upgrade(isolated_database, "0019")
    proxy = _create_proxy_bundle(isolated_database)
    _alias_proxy_to_production_bundle(proxy, monkeypatch)
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(
                'ALTER TABLE "public"."pinterest_learning_snapshots" '
                "ADD COLUMN one_fingerprint_drift integer"
            ))
            with pytest.raises(SchemaAdoptionRefused, match="exact Issue #117"):
                reconcile_preapplied_bundle(connection, "0020")
    finally:
        engine.dispose()


def test_partial_bundle_presence_refuses(
    isolated_database: str,
) -> None:
    _upgrade(isolated_database, "0019")
    _create_proxy_bundle(isolated_database)
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(
                'DROP TABLE "public"."pinterest_autonomous_destination_runs"'
            ))
            with pytest.raises(SchemaAdoptionRefused, match="partial table presence"):
                reconcile_preapplied_bundle(connection, "0020")
    finally:
        engine.dispose()


def test_complete_bundle_with_wrong_bookkeeping_refuses(
    isolated_database: str,
) -> None:
    _upgrade(isolated_database, "0019")
    _create_proxy_bundle(isolated_database)
    _set_revision(isolated_database, "0018")
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            with pytest.raises(
                SchemaAdoptionRefused,
                match="requires Alembic revision 0019",
            ):
                reconcile_preapplied_bundle(connection, "0020")
    finally:
        engine.dispose()


@pytest.mark.parametrize("table", BUNDLE_TABLES)
def test_every_nonempty_bundle_member_refuses(
    isolated_database: str,
    monkeypatch: pytest.MonkeyPatch,
    table: str,
) -> None:
    _upgrade(isolated_database, "0019")
    proxy = _create_proxy_bundle(isolated_database)
    _alias_proxy_to_production_bundle(proxy, monkeypatch)
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            _insert_generic_row(connection, table)
            with pytest.raises(SchemaAdoptionRefused, match=f"{table} is not empty"):
                reconcile_preapplied_bundle(connection, "0020")
    finally:
        engine.dispose()


def test_external_foreign_key_refuses(
    isolated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _upgrade(isolated_database, "0019")
    proxy = _create_proxy_bundle(isolated_database)
    _alias_proxy_to_production_bundle(proxy, monkeypatch)
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(
                'CREATE SCHEMA "outside_schema"'
            ))
            connection.execute(sa.text(
                'CREATE TABLE "outside_schema"."outside_bundle" ('
                "id integer PRIMARY KEY, "
                'plan_id varchar(36) REFERENCES '
                '"public"."pinterest_portfolio_plans"(id)'
                ")"
            ))
            with pytest.raises(
                SchemaAdoptionRefused,
                match="unexpected external dependencies",
            ):
                reconcile_preapplied_bundle(connection, "0020")
    finally:
        engine.dispose()


@pytest.mark.parametrize("drift", ("presence", "fingerprint", "row"))
def test_lock_recheck_refuses_concurrent_drift(
    isolated_database: str,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    _upgrade(isolated_database, "0019")
    proxy = _create_proxy_bundle(isolated_database)
    _alias_proxy_to_production_bundle(proxy, monkeypatch)
    engine = sa.create_engine(isolated_database)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()

            def mutate_during_lock(bind, _tables):
                if drift == "presence":
                    bind.execute(sa.text(
                        'DROP TABLE "public".'
                        '"pinterest_autonomous_destination_runs"'
                    ))
                elif drift == "fingerprint":
                    bind.execute(sa.text(
                        'ALTER TABLE "public"."pinterest_learning_snapshots" '
                        "ADD COLUMN concurrent_drift integer"
                    ))
                else:
                    _insert_generic_row(bind, "pinterest_learning_snapshots")

            monkeypatch.setattr(
                migration_adoption,
                "_lock_owned_tables",
                mutate_during_lock,
            )
            expected = {
                "presence": "changed while locking",
                "fingerprint": "exact Issue #117",
                "row": "is not empty",
            }[drift]
            with pytest.raises(SchemaAdoptionRefused, match=expected):
                reconcile_preapplied_bundle(connection, "0020")
            transaction.rollback()
    finally:
        engine.dispose()


def test_rewrite_dependency_refuses_before_teardown(
    isolated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _upgrade(isolated_database, "0019")
    proxy = _create_proxy_bundle(isolated_database)
    _alias_proxy_to_production_bundle(proxy, monkeypatch)
    engine = sa.create_engine(isolated_database)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            connection.execute(sa.text(
                'CREATE VIEW "bundle_dependency_view" AS '
                'SELECT id FROM "pinterest_portfolio_plans"'
            ))
            with pytest.raises(
                SchemaAdoptionRefused,
                match="unexpected external dependencies",
            ):
                reconcile_preapplied_bundle(connection, "0020")
            transaction.rollback()
        _assert_proxy_bundle(isolated_database, proxy)
    finally:
        engine.dispose()


def test_post_rebuild_mismatch_rolls_back_entire_bundle(
    isolated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _upgrade(isolated_database, "0019")
    proxy = _create_proxy_bundle(isolated_database)
    _alias_proxy_to_production_bundle(proxy, monkeypatch)
    monkeypatch.setitem(
        FROZEN_FINGERPRINTS,
        "pinterest_autonomous_destination_runs",
        "0" * 64,
    )

    with pytest.raises(
        SchemaAdoptionRefused,
        match="did not produce all frozen canonical contracts",
    ):
        _upgrade(isolated_database, "head")

    assert _revision(isolated_database) == "0019"
    _assert_proxy_bundle(isolated_database, proxy)


def test_teardown_ddl_failure_rolls_back_entire_bundle(
    isolated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _upgrade(isolated_database, "0019")
    proxy = _create_proxy_bundle(isolated_database)
    _alias_proxy_to_production_bundle(proxy, monkeypatch)

    def fail_during_teardown(connection):
        connection.execute(sa.text(
            'DROP TABLE "public"."pinterest_autonomous_destination_runs"'
        ))
        raise RuntimeError("injected teardown DDL failure")

    monkeypatch.setattr(
        migration_adoption,
        "_drop_preapplied_bundle_tables",
        fail_during_teardown,
    )
    with pytest.raises(RuntimeError, match="injected teardown DDL failure"):
        _upgrade(isolated_database, "head")

    assert _revision(isolated_database) == "0019"
    _assert_proxy_bundle(isolated_database, proxy)


def test_drop_order_is_fixed_reverse_dependency_order() -> None:
    assert PREAPPLIED_BUNDLE_DROP_ORDER == (
        "pinterest_autonomous_destination_runs",
        "pinterest_autonomous_execution_runs",
        "pinterest_optimizer_applications",
        "pinterest_learning_snapshots",
        "pinterest_analytics_ingestion_runs",
        "pinterest_analytics_snapshots",
        "pinterest_autonomous_generation_runs",
        "pinterest_seo_briefs",
        "pinterest_portfolio_plan_items",
        "pinterest_portfolio_plans",
    )


def test_non_postgresql_refuses_preapplied_bundle() -> None:
    engine = sa.create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(
                "CREATE TABLE pinterest_seo_briefs "
                "(id VARCHAR(36) PRIMARY KEY)"
            ))
            with pytest.raises(SchemaAdoptionRefused, match="requires PostgreSQL"):
                reconcile_preapplied_bundle(connection, "0020")
    finally:
        engine.dispose()

def test_partial_target_rolls_back_entire_bundle(
    isolated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repaired bundle may not commit unless the full 0027 contract exists."""
    _upgrade(isolated_database, "0019")
    proxy = _create_proxy_bundle(isolated_database)
    _alias_proxy_to_production_bundle(proxy, monkeypatch)

    with pytest.raises(
        SchemaAdoptionRefused,
        match="reconciled bundle was not fully recreated",
    ):
        _upgrade(isolated_database, "0025")

    assert _revision(isolated_database) == "0019"
    _assert_proxy_bundle(isolated_database, proxy)

