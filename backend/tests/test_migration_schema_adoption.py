"""PostgreSQL certification for Replit pre-applied migration adoption."""
from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url

import app.db.migration_adoption as migration_adoption
from app.db.migration_adoption import (
    FROZEN_FINGERPRINTS,
    LEGACY_0020_FINGERPRINTS,
    SchemaAdoptionRefused,
    adopt_preapplied_revision,
)


POSTGRES_URL = os.getenv("TASK58_CATALOG_URL") or os.getenv("TASK58_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL, reason="TASK58_POSTGRES_URL is required for PostgreSQL adoption tests"
)
BACKEND = Path(__file__).resolve().parents[1]


def _admin_url(url: str) -> str:
    parsed = make_url(url)
    return parsed.set(database="postgres").render_as_string(hide_password=False)


@pytest.fixture
def isolated_database() -> Iterator[str]:
    """Provision a unique database; scenarios never share migration state."""
    name = f"task58_{uuid4().hex[:16]}"
    admin = sa.create_engine(_admin_url(POSTGRES_URL), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
        parsed = make_url(POSTGRES_URL)
        yield parsed.set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as connection:
            connection.execute(sa.text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname=:database AND pid <> pg_backend_pid()"
            ), {"database": name})
            connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


def _alembic(url: str, target: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", target],
        cwd=BACKEND,
        env={**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(BACKEND)},
        text=True,
        capture_output=True,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    return output


def _current(url: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "current"],
        cwd=BACKEND,
        env={**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(BACKEND)},
        text=True,
        capture_output=True,
        check=True,
    )
    lines = [line.strip() for line in (result.stdout + result.stderr).splitlines()]
    return next(line for line in lines if line == "0027 (head)")


def _set_bookkeeping(url: str, revision: str) -> None:
    engine = sa.create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE alembic_version SET version_num=:revision"),
                {"revision": revision},
            )
    finally:
        engine.dispose()


def _drop_table(url: str, table: str) -> None:
    engine = sa.create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(f'DROP TABLE "{table}"'))
    finally:
        engine.dispose()


def _failed_alembic(url: str, target: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", target],
        cwd=BACKEND,
        env={**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(BACKEND)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    return result.stdout + result.stderr


def _assert_frozen_owned_tables(url: str) -> None:
    engine = sa.create_engine(url)
    try:
        with engine.begin() as connection:
            for revision in ("0020", "0021", "0022", "0023", "0024", "0025", "0026", "0027"):
                assert adopt_preapplied_revision(connection, revision) is True
    finally:
        engine.dispose()


def _make_legacy_0020(
    connection: sa.Connection,
    *,
    plans: bool = True,
    items: bool = True,
) -> None:
    if plans:
        connection.execute(sa.text(
            """ALTER TABLE "public"."pinterest_portfolio_plans"
            DROP CONSTRAINT "ck_pinterest_portfolio_plan_status" """
        ))
        connection.execute(sa.text(
            """ALTER TABLE "public"."pinterest_portfolio_plans"
            ADD CONSTRAINT "ck_pinterest_portfolio_plan_status"
            CHECK (
              status::text = ANY (
                ARRAY[
                  'DRAFT'::character varying::text,
                  'ACTIVE'::character varying::text,
                  'COMPLETED'::character varying::text,
                  'CANCELLED'::character varying::text
                ]
              )
            )"""
        ))
        connection.execute(sa.text(
            'DROP INDEX "public"."uq_pinterest_portfolio_active_month"'
        ))
        connection.execute(sa.text(
            """CREATE UNIQUE INDEX "uq_pinterest_portfolio_active_month"
            ON "public"."pinterest_portfolio_plans" ("store_id", "month_start")
            WHERE (
              status::text = ANY (
                ARRAY[
                  'DRAFT'::character varying::text,
                  'ACTIVE'::character varying::text
                ]
              )
            )"""
        ))
    if items:
        connection.execute(sa.text(
            """ALTER TABLE "public"."pinterest_portfolio_plan_items"
            DROP CONSTRAINT "ck_pinterest_portfolio_plan_item_status" """
        ))
        connection.execute(sa.text(
            """ALTER TABLE "public"."pinterest_portfolio_plan_items"
            ADD CONSTRAINT "ck_pinterest_portfolio_plan_item_status"
            CHECK (
              status::text = ANY (
                ARRAY[
                  'PLANNED'::character varying::text,
                  'PROMOTED'::character varying::text,
                  'GENERATED'::character varying::text,
                  'SCHEDULED'::character varying::text,
                  'PUBLISHED'::character varying::text,
                  'FAILED'::character varying::text,
                  'SKIPPED'::character varying::text
                ]
              )
            )"""
        ))


def _0020_fingerprints(connection: sa.Connection) -> dict[str, str]:
    return migration_adoption._table_fingerprints(
        connection,
        migration_adoption.OWNED_TABLES["0020"],
    )


def _prepare_legacy_0020(url: str) -> None:
    _alembic(url, "0020")
    _set_bookkeeping(url, "0019")
    engine = sa.create_engine(url)
    try:
        with engine.begin() as connection:
            _make_legacy_0020(connection)
            assert _0020_fingerprints(connection) == LEGACY_0020_FINGERPRINTS
    finally:
        engine.dispose()


def test_fresh_postgresql_upgrade_reaches_exact_head(isolated_database: str) -> None:
    _alembic(isolated_database, "head")
    assert _current(isolated_database) == "0027 (head)"
    _assert_frozen_owned_tables(isolated_database)


def test_simulated_production_0020_is_adopted(isolated_database: str) -> None:
    _alembic(isolated_database, "0019")
    _alembic(isolated_database, "0020")
    _set_bookkeeping(isolated_database, "0019")
    _alembic(isolated_database, "head")
    assert _current(isolated_database) == "0027 (head)"
    _assert_frozen_owned_tables(isolated_database)


def test_exact_legacy_0020_repairs_and_reaches_head(
    isolated_database: str,
) -> None:
    _prepare_legacy_0020(isolated_database)
    _alembic(isolated_database, "head")
    assert _current(isolated_database) == "0027 (head)"
    _assert_frozen_owned_tables(isolated_database)


@pytest.mark.parametrize(
    ("table", "insert_sql"),
    (
        (
            "pinterest_portfolio_plans",
            """INSERT INTO pinterest_portfolio_plans (
              id, store_id, month_start, month_end, target_pins,
              policy_version, input_fingerprint, plan_fingerprint,
              metadata_json
            ) VALUES (
              'legacy-plan', 'missing-store', DATE '2026-01-01',
              DATE '2026-01-31', 1, 'legacy', 'input', 'plan', '{}'::json
            )""",
        ),
        (
            "pinterest_portfolio_plan_items",
            """INSERT INTO pinterest_portfolio_plan_items (
              id, plan_id, slot_index, product_id, local_board_id,
              board_key_snapshot, content_angle_id, angle_key_snapshot,
              seed_keywords, selection_score, selection_metadata,
              item_fingerprint
            ) VALUES (
              'legacy-item', 'missing-plan', 0, 'missing-product',
              'missing-board', 'board', 'missing-angle', 'angle',
              '[]'::json, 0, '{}'::json, 'item'
            )""",
        ),
    ),
)
def test_nonempty_legacy_0020_refuses_before_repair(
    isolated_database: str,
    table: str,
    insert_sql: str,
) -> None:
    _prepare_legacy_0020(isolated_database)
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(
                "SET LOCAL session_replication_role = replica"
            ))
            connection.execute(sa.text(insert_sql))
    finally:
        engine.dispose()

    output = _failed_alembic(isolated_database, "head")
    assert f"{table} is not empty" in output

    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            assert _0020_fingerprints(connection) == LEGACY_0020_FINGERPRINTS
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("plans_legacy", "items_legacy"),
    ((True, False), (False, True)),
)
def test_mixed_legacy_and_canonical_0020_refuses(
    isolated_database: str,
    plans_legacy: bool,
    items_legacy: bool,
) -> None:
    _alembic(isolated_database, "0020")
    _set_bookkeeping(isolated_database, "0019")
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            _make_legacy_0020(
                connection,
                plans=plans_legacy,
                items=items_legacy,
            )
            fingerprints = _0020_fingerprints(connection)
            for table, legacy in (
                ("pinterest_portfolio_plans", plans_legacy),
                ("pinterest_portfolio_plan_items", items_legacy),
            ):
                expected = (
                    LEGACY_0020_FINGERPRINTS[table]
                    if legacy
                    else FROZEN_FINGERPRINTS[table]
                )
                assert fingerprints[table] == expected
            with pytest.raises(SchemaAdoptionRefused):
                adopt_preapplied_revision(connection, "0020")
    finally:
        engine.dispose()


def test_unknown_0020_drift_does_not_enter_repair(
    isolated_database: str,
) -> None:
    _prepare_legacy_0020(isolated_database)
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(
                """ALTER TABLE "public"."pinterest_portfolio_plans"
                ADD COLUMN "unknown_drift" integer"""
            ))
            with pytest.raises(SchemaAdoptionRefused):
                adopt_preapplied_revision(connection, "0020")
    finally:
        engine.dispose()


def test_0020_lock_recheck_detects_presence_change(
    isolated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _alembic(isolated_database, "0020")
    _set_bookkeeping(isolated_database, "0019")
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            def change_presence(
                locked_connection: sa.Connection,
                owned: tuple[str, ...],
            ) -> None:
                assert locked_connection is connection
                assert owned == migration_adoption.OWNED_TABLES["0020"]
                locked_connection.execute(sa.text(
                    'DROP TABLE "public"."pinterest_portfolio_plan_items"'
                ))

            monkeypatch.setattr(
                migration_adoption,
                "_lock_owned_tables",
                change_presence,
            )
            with pytest.raises(
                SchemaAdoptionRefused,
                match="table presence changed while locking",
            ):
                with connection.begin_nested():
                    adopt_preapplied_revision(connection, "0020")
    finally:
        engine.dispose()


def test_0020_post_repair_mismatch_rolls_back(
    isolated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_legacy_0020(isolated_database)
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            original_repair = migration_adoption._repair_legacy_0020

            def repair_with_drift(repair_connection: sa.Connection) -> None:
                original_repair(repair_connection)
                repair_connection.execute(sa.text(
                    """CREATE INDEX "unexpected_post_repair_index"
                    ON "public"."pinterest_portfolio_plans" ("month_end")"""
                ))

            monkeypatch.setattr(
                migration_adoption,
                "_repair_legacy_0020",
                repair_with_drift,
            )
            with pytest.raises(
                SchemaAdoptionRefused,
                match="repair did not produce its frozen contracts",
            ):
                with connection.begin_nested():
                    adopt_preapplied_revision(connection, "0020")
            assert _0020_fingerprints(connection) == LEGACY_0020_FINGERPRINTS
    finally:
        engine.dispose()


def test_0020_unexpected_repair_failure_rolls_back(
    isolated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_legacy_0020(isolated_database)
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            def interrupted_repair(repair_connection: sa.Connection) -> None:
                repair_connection.execute(sa.text(
                    """ALTER TABLE "public"."pinterest_portfolio_plans"
                    DROP CONSTRAINT "ck_pinterest_portfolio_plan_status" """
                ))
                raise RuntimeError("unexpected dependency or repair failure")

            monkeypatch.setattr(
                migration_adoption,
                "_repair_legacy_0020",
                interrupted_repair,
            )
            with pytest.raises(
                RuntimeError,
                match="unexpected dependency or repair failure",
            ):
                with connection.begin_nested():
                    adopt_preapplied_revision(connection, "0020")
            assert _0020_fingerprints(connection) == LEGACY_0020_FINGERPRINTS
    finally:
        engine.dispose()


def test_0020_database_blocker_rolls_back_migration(
    isolated_database: str,
) -> None:
    _prepare_legacy_0020(isolated_database)
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(
                """CREATE FUNCTION block_0020_drop_index()
                RETURNS event_trigger
                LANGUAGE plpgsql
                AS $$
                BEGIN
                  IF TG_TAG = 'DROP INDEX' THEN
                    RAISE EXCEPTION 'Issue 115 test blocked DROP INDEX';
                  END IF;
                END;
                $$"""
            ))
            connection.execute(sa.text(
                """CREATE EVENT TRIGGER block_0020_repair
                ON ddl_command_start
                EXECUTE FUNCTION block_0020_drop_index()"""
            ))

        output = _failed_alembic(isolated_database, "head")
        assert "Issue 115 test blocked DROP INDEX" in output

        with engine.begin() as connection:
            connection.execute(sa.text(
                "DROP EVENT TRIGGER block_0020_repair"
            ))
            connection.execute(sa.text(
                "DROP FUNCTION block_0020_drop_index()"
            ))
            revision = connection.execute(sa.text(
                "SELECT version_num FROM alembic_version"
            )).scalar_one()
            assert revision == "0019"
            assert _0020_fingerprints(connection) == LEGACY_0020_FINGERPRINTS
    finally:
        engine.dispose()


def test_full_preapply_adopts_each_revision_sequentially(
    isolated_database: str,
) -> None:
    _alembic(isolated_database, "0019")
    _alembic(isolated_database, "head")
    _set_bookkeeping(isolated_database, "0019")
    output = _alembic(isolated_database, "head")
    assert _current(isolated_database) == "0027 (head)"
    _assert_frozen_owned_tables(isolated_database)
    for revision in ("0020", "0021", "0022", "0023", "0024", "0025", "0026", "0027"):
        assert f"Running upgrade" in output
        assert revision in output


@pytest.mark.parametrize(
    "dropped_table",
    ("pinterest_portfolio_plans", "pinterest_portfolio_plan_items"),
)
def test_partial_0020_refuses_before_bookkeeping(
    isolated_database: str,
    dropped_table: str,
) -> None:
    _alembic(isolated_database, "0019")
    _alembic(isolated_database, "0020")
    _set_bookkeeping(isolated_database, "0019")
    if dropped_table == "pinterest_portfolio_plans":
        engine = sa.create_engine(isolated_database)
        try:
            with engine.begin() as connection:
                connection.execute(sa.text(
                    'DROP TABLE "pinterest_portfolio_plans" CASCADE'
                ))
        finally:
            engine.dispose()
    else:
        _drop_table(isolated_database, dropped_table)
    output = _failed_alembic(isolated_database, "head")
    assert "partial table presence" in output


def test_partial_0023_refuses_before_bookkeeping(isolated_database: str) -> None:
    _alembic(isolated_database, "0023")
    _set_bookkeeping(isolated_database, "0022")
    _drop_table(isolated_database, "pinterest_analytics_ingestion_runs")
    output = _failed_alembic(isolated_database, "head")
    assert "partial table presence" in output

REVISION_TABLES = (
    ("0020", "pinterest_portfolio_plans"),
    ("0020", "pinterest_portfolio_plan_items"),
    ("0021", "pinterest_seo_briefs"),
    ("0022", "pinterest_autonomous_generation_runs"),
    ("0023", "pinterest_analytics_snapshots"),
    ("0023", "pinterest_analytics_ingestion_runs"),
    ("0024", "pinterest_learning_snapshots"),
    ("0025", "pinterest_optimizer_applications"),
    ("0026", "pinterest_autonomous_execution_runs"),
    ("0027", "pinterest_autonomous_destination_runs"),
)


def test_canonical_tables_are_adoptable_and_empty() -> None:
    engine = sa.create_engine(POSTGRES_URL)
    try:
        with engine.begin() as connection:
            for revision in ("0020", "0021", "0022", "0023", "0024", "0025", "0026", "0027"):
                assert adopt_preapplied_revision(connection, revision) is True
    finally:
        engine.dispose()


def test_partial_multi_table_revision_fails_closed() -> None:
    engine = sa.create_engine(POSTGRES_URL)
    try:
        with engine.begin() as connection:
            with pytest.raises(SchemaAdoptionRefused):
                with connection.begin_nested():
                    connection.execute(sa.text(
                        'DROP TABLE IF EXISTS "public"."pinterest_analytics_ingestion_runs"'
                    ))
                    adopt_preapplied_revision(connection, "0023")
    finally:
        engine.dispose()


@pytest.mark.parametrize(("revision", "table"), REVISION_TABLES)
def test_each_revision_structural_drift_fails_closed(revision: str, table: str) -> None:
    engine = sa.create_engine(POSTGRES_URL)
    try:
        with engine.begin() as connection:
            with pytest.raises(SchemaAdoptionRefused):
                with connection.begin_nested():
                    connection.execute(sa.text(
                        f'ALTER TABLE "public"."{table}" ADD COLUMN adoption_drift integer'
                    ))
                    adopt_preapplied_revision(connection, revision)
    finally:
        engine.dispose()


@pytest.mark.parametrize(("revision", "table"), REVISION_TABLES)
def test_each_revision_nonempty_table_fails_closed(revision: str, table: str) -> None:
    engine = sa.create_engine(POSTGRES_URL)
    try:
        with engine.begin() as connection:
            with pytest.raises(SchemaAdoptionRefused):
                with connection.begin_nested():
                    columns = connection.execute(sa.text(
                        """SELECT column_name, data_type FROM information_schema.columns
                        WHERE table_schema='public' AND table_name=:table
                        ORDER BY ordinal_position"""
                    ), {"table": table}).all()
                    values = []
                    for column, data_type in columns:
                        if column == "status":
                            status = {
                                "pinterest_portfolio_plans": "DRAFT",
                                "pinterest_portfolio_plan_items": "PLANNED",
                                "pinterest_seo_briefs": "CURRENT",
                                "pinterest_optimizer_applications": "APPLIED",
                            }.get(table, "STARTED")
                            values.append(f"'{status}'")
                        elif column == "stage":
                            values.append("'STARTED'")
                        elif column == "observation_window":
                            values.append("'D1'")
                        elif data_type in {"character varying", "text"}:
                            values.append("'x'")
                        elif data_type == "date":
                            values.append("CURRENT_DATE")
                        elif data_type.startswith("timestamp"):
                            values.append("CURRENT_TIMESTAMP")
                        elif data_type in {"integer", "numeric", "bigint"}:
                            values.append("0")
                        elif data_type == "boolean":
                            values.append("false")
                        elif data_type == "json":
                            values.append("'{}'::json")
                        else:
                            values.append("NULL")
                    connection.execute(sa.text(
                        "SET LOCAL session_replication_role = replica"
                    ))
                    connection.execute(sa.text(
                        f'INSERT INTO "public"."{table}" '
                        f'({", ".join(f"""\"{column}\"""" for column, _ in columns)}) '
                        f'VALUES ({", ".join(values)})'
                    ))
                    adopt_preapplied_revision(connection, revision)
    finally:
        engine.dispose()


def _assert_drift(url: str, statements: list[str], revision: str) -> None:
    engine = sa.create_engine(url)
    try:
        with engine.begin() as connection:
            with pytest.raises(SchemaAdoptionRefused):
                with connection.begin_nested():
                    for statement in statements:
                        connection.execute(sa.text(statement))
                    adopt_preapplied_revision(connection, revision)
    finally:
        engine.dispose()


def test_adversarial_type_and_default_drift_refuses() -> None:
    _assert_drift(
        POSTGRES_URL,
        ['ALTER TABLE "public"."pinterest_portfolio_plans" '
         'ALTER COLUMN "policy_version" TYPE text'],
        "0020",
    )
    _assert_drift(
        POSTGRES_URL,
        ['ALTER TABLE "public"."pinterest_portfolio_plans" '
         'ALTER COLUMN "status" SET DEFAULT \'ACTIVE\''],
        "0020",
    )


def test_adversarial_fk_and_check_drift_refuses() -> None:
    _assert_drift(
        POSTGRES_URL,
        ['ALTER TABLE "public"."pinterest_portfolio_plan_items" '
         'DROP CONSTRAINT "pinterest_portfolio_plan_items_plan_id_fkey"'],
        "0020",
    )
    _assert_drift(
        POSTGRES_URL,
        ['ALTER TABLE "public"."pinterest_portfolio_plans" '
         'DROP CONSTRAINT "ck_pinterest_portfolio_plan_status"'],
        "0020",
    )


def test_adversarial_unique_and_named_index_drift_refuses() -> None:
    _assert_drift(
        POSTGRES_URL,
        ['ALTER TABLE "public"."pinterest_portfolio_plan_items" '
         'DROP CONSTRAINT "uq_pinterest_portfolio_item_fingerprint"'],
        "0020",
    )
    _assert_drift(
        POSTGRES_URL,
        ['ALTER INDEX "public"."ix_pinterest_portfolio_plans_status" '
         'RENAME TO "adoption_renamed_index"'],
        "0020",
    )


def test_adversarial_partial_predicate_and_unexpected_objects_refuse() -> None:
    _assert_drift(
        POSTGRES_URL,
        [
            'DROP INDEX "public"."uq_pinterest_portfolio_active_month"',
            'CREATE UNIQUE INDEX "uq_pinterest_portfolio_active_month" '
            'ON "public"."pinterest_portfolio_plans" ("store_id", "month_start") '
            "WHERE status = 'DRAFT'",
        ],
        "0020",
    )
    _assert_drift(
        POSTGRES_URL,
        ['CREATE INDEX "adoption_unexpected_index" '
         'ON "public"."pinterest_portfolio_plans" ("month_end")'],
        "0020",
    )
    _assert_drift(
        POSTGRES_URL,
        ['ALTER TABLE "public"."pinterest_portfolio_plans" '
         'ADD CONSTRAINT "adoption_unexpected_check" CHECK (target_pins >= 0)'],
        "0020",
    )


def test_unsupported_adoption_revision_refuses() -> None:
    engine = sa.create_engine(POSTGRES_URL)
    try:
        with engine.begin() as connection:
            with pytest.raises(SchemaAdoptionRefused):
                adopt_preapplied_revision(connection, "0019")
    finally:
        engine.dispose()


def test_adoption_locks_owned_tables_before_validation() -> None:
    engine = sa.create_engine(POSTGRES_URL)
    connection = engine.connect()
    transaction = connection.begin()
    try:
        assert adopt_preapplied_revision(connection, "0020") is True
        locked = connection.execute(sa.text(
            """SELECT count(*) FROM pg_locks l
            JOIN pg_class c ON c.oid=l.relation
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE l.pid=pg_backend_pid() AND l.granted
              AND l.mode='AccessExclusiveLock'
              AND n.nspname='public'
              AND c.relname IN ('pinterest_portfolio_plans',
                                'pinterest_portfolio_plan_items')"""
        )).scalar_one()
        assert int(locked) == 2
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def test_non_postgres_fresh_path_is_not_adoption() -> None:
    engine = sa.create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            assert adopt_preapplied_revision(connection, "0020") is False
    finally:
        engine.dispose()


def test_database_url_rewrite_preserves_credentials() -> None:
    url = "postgresql+psycopg://user:password@127.0.0.1:5432/source"
    assert _admin_url(url) == (
        "postgresql+psycopg://user:password@127.0.0.1:5432/postgres"
    )


def test_non_postgres_preapplied_schema_is_rejected() -> None:
    engine = sa.create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(
                "CREATE TABLE pinterest_seo_briefs (id VARCHAR(36) PRIMARY KEY)"
            ))
            with pytest.raises(SchemaAdoptionRefused):
                adopt_preapplied_revision(connection, "0021")
    finally:
        engine.dispose()