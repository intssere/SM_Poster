"""Only disposable PostgreSQL databases are used for managed-schema adoption."""
from __future__ import annotations

import os
import importlib.util
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url

from app.db.migration_adoption import (
    SchemaAdoptionRefused,
    adopt_managed_preapplied_0031,
    verify_frozen_schema_at_head,
)
from app.db.session import sqlalchemy_database_url
from app.db import managed_schema_adoption, schema_canonicality_guard


BACKEND = Path(__file__).resolve().parents[1]
POSTGRES_URL = os.getenv("TASK60_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL, reason="TASK60_POSTGRES_URL disposable PostgreSQL required"
)


@pytest.fixture
def database():
    name = f"task60_{uuid4().hex[:16]}"
    base_url = make_url(sqlalchemy_database_url(POSTGRES_URL))
    admin_url = base_url.set(database="postgres")
    admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
        url = base_url.set(database=name)
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "0031"],
            cwd=BACKEND,
            env={**os.environ, "DATABASE_URL": url.render_as_string(hide_password=False),
                 "PYTHONPATH": str(BACKEND)},
            check=True,
            capture_output=True,
        )
        engine = sa.create_engine(url)
        try:
            with engine.begin() as connection:
                connection.execute(sa.text(
                    "UPDATE alembic_version SET version_num='0030' "
                    "WHERE version_num='0031'"
                ))
            yield engine
        finally:
            engine.dispose()
    finally:
        with admin.connect() as connection:
            connection.execute(sa.text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname=:name AND pid <> pg_backend_pid()"
            ), {"name": name})
            connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


def revision(engine):
    with engine.connect() as connection:
        return connection.execute(
            sa.text('SELECT version_num FROM "public"."alembic_version" ORDER BY version_num')
        ).scalars().all()


def adopt(engine):
    with engine.begin() as connection:
        return adopt_managed_preapplied_0031(connection)


def test_exact_preapplied_schema_adopts_only_bookkeeping_and_is_idempotent(database):
    statements = []
    def record(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.strip().upper())
    sa.event.listen(database, "before_cursor_execute", record)
    try:
        assert adopt(database) is True
        with database.connect() as connection:
            verify_frozen_schema_at_head(connection, revision="0031")
        assert adopt(database) is False
    finally:
        sa.event.remove(database, "before_cursor_execute", record)
    writes = [s for s in statements if s.startswith(("INSERT ", "UPDATE ", "DELETE ",
                                                       "CREATE ", "ALTER ", "DROP "))]
    assert writes == ['UPDATE "PUBLIC"."ALEMBIC_VERSION" SET VERSION_NUM=\'0031\' '
                      "WHERE VERSION_NUM='0030' RETURNING VERSION_NUM"]
    assert revision(database) == ["0031"]


@pytest.mark.parametrize("ddl", [
    'DROP TABLE routine_scheduled_quota_reservations',
    'ALTER TABLE routine_scheduled_quota_reservations ALTER COLUMN vendor_key TYPE varchar(254)',
    'ALTER TABLE routine_scheduled_quota_reservations ALTER COLUMN reserved_at DROP DEFAULT',
    'DROP INDEX ix_routine_scheduled_quota_day',
    'ALTER TABLE routine_scheduled_quota_reservations DROP CONSTRAINT fk_routine_scheduled_quota_plan',
    'ALTER TABLE routine_scheduled_quota_reservations DROP CONSTRAINT uq_routine_scheduled_quota_publication',
    'ALTER TABLE pinterest_autonomous_run_reconciliations ADD COLUMN unexpected integer',
])
def test_partial_and_structural_drift_refuse_without_bookkeeping(database, ddl):
    with database.begin() as connection:
        connection.execute(sa.text(ddl))
    with pytest.raises((SchemaAdoptionRefused, sa.exc.DBAPIError)):
        adopt(database)
    assert revision(database) == ["0030"]


@pytest.mark.parametrize("versions", [
    (), ("0029",), ("0032",), ("0030", "0032"),
    ("0030", "0031"), ("0031", "0032"),
])
def test_wrong_or_ambiguous_revision_refuses(database, versions):
    with database.begin() as connection:
        connection.execute(sa.text("DELETE FROM alembic_version"))
        for version in versions:
            connection.execute(sa.text(
                "INSERT INTO alembic_version (version_num) VALUES (:revision)"
            ), {"revision": version})
    expected = "0032 readiness admissions table missing" if versions == ("0032",) else "exactly one Alembic revision"
    with pytest.raises(SchemaAdoptionRefused, match=expected):
        adopt(database)
    assert revision(database) == list(versions)


def test_nonpaused_routine_control_refuses(database):
    with database.begin() as connection:
        connection.execute(sa.text(
            "UPDATE routine_publishing_control SET state='DRY_RUN' WHERE id='default'"
        ))
    with pytest.raises(SchemaAdoptionRefused, match="PAUSED"):
        adopt(database)
    assert revision(database) == ["0030"]


def test_missing_transaction_or_non_read_committed_is_refused(database):
    with database.connect() as connection:
        with pytest.raises(SchemaAdoptionRefused, match="requires a transaction"):
            adopt_managed_preapplied_0031(connection)
    with database.connect().execution_options(isolation_level="SERIALIZABLE") as connection:
        with connection.begin():
            with pytest.raises(SchemaAdoptionRefused, match="READ COMMITTED"):
                adopt_managed_preapplied_0031(connection)
    assert revision(database) == ["0030"]


@pytest.mark.parametrize("status", ["PUBLISH_UNKNOWN", "PUBLISHING"])
def test_publication_activity_refuses(database, status):
    with database.begin() as connection:
        # Disposable superuser-owned database only: model a live row without
        # creating an unrelated editorial/provider fixture graph.
        connection.execute(sa.text("SET LOCAL session_replication_role = replica"))
        connection.execute(sa.text(
            "INSERT INTO pin_publications (id, draft_id, creative_id, "
            "publication_fingerprint, status, provider_response) "
            "VALUES ('pub', 'draft', 'creative', 'fingerprint', :status, '{}')"
        ), {"status": status})
    with pytest.raises(SchemaAdoptionRefused, match=status):
        adopt(database)
    assert revision(database) == ["0030"]


def test_running_routine_run_refuses(database):
    with database.begin() as connection:
        connection.execute(sa.text(
            "INSERT INTO routine_publishing_runs (id, mode, started_at, status, "
            "scanned, eligible, skipped, claimed, dispatched, published, failed, unknown, metadata_json) "
            "VALUES ('run', 'DRY_RUN', now(), 'RUNNING', 0, 0, 0, 0, 0, 0, 0, 0, '{}')"
        ))
    with pytest.raises(SchemaAdoptionRefused, match="running routine runs"):
        adopt(database)
    assert revision(database) == ["0030"]


def test_nonempty_quota_evidence_refuses(database):
    with database.begin() as connection:
        connection.execute(sa.text("SET LOCAL session_replication_role = replica"))
        connection.execute(sa.text(
            "INSERT INTO routine_scheduled_quota_reservations "
            "(id, publication_id, plan_id, plan_item_id, product_id, vendor_key, "
            "board_id, scheduled_for, month_start) VALUES "
            "('reservation', 'pub', 'plan', 'item', 'product', 'vendor', "
            "'board', CURRENT_DATE, date_trunc('month', CURRENT_DATE)::date)"
        ))
    with pytest.raises(SchemaAdoptionRefused, match="must be empty"):
        adopt(database)
    assert revision(database) == ["0030"]


def test_active_permit_refuses(database):
    with database.begin() as connection:
        connection.execute(sa.text("SET LOCAL session_replication_role = replica"))
        connection.execute(sa.text(
            "INSERT INTO routine_dispatch_permits "
            "(id, publication_id, dispatch_provider, approval_id, "
            "pinterest_board_record_id, publication_fingerprint, request_fingerprint, "
            "scheduled_for_snapshot, quality_policy_version, quality_snapshot, "
            "duplicate_snapshot, readiness_snapshot, authorized_by, authorized_at, "
            "expires_at, status) VALUES "
            "('permit', 'pub', 'buffer', 'approval', 'board', 'fingerprint', "
            "'request', now(), 'v1', '{}', '{}', '{}', 'test', now(), "
            "now() + interval '1 hour', 'ACTIVE')"
        ))
    with pytest.raises(SchemaAdoptionRefused, match="active permits"):
        adopt(database)
    assert revision(database) == ["0030"]


def test_transaction_rollback_keeps_revision_0030(database):
    with pytest.raises(RuntimeError, match="simulated crash"):
        with database.begin() as connection:
            assert adopt_managed_preapplied_0031(connection)
            raise RuntimeError("simulated crash")
    assert revision(database) == ["0030"]
    assert adopt(database) is True


def test_concurrent_adopter_refuses_contention_and_updates_once(database):
    with database.begin() as first:
        assert adopt_managed_preapplied_0031(first) is True
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(adopt, database)
            with pytest.raises(SchemaAdoptionRefused, match="lock or table availability"):
                future.result(timeout=10)
    assert revision(database) == ["0031"]
    assert adopt(database) is False


def test_already_0031_with_schema_drift_refuses(database):
    assert adopt(database)
    with database.begin() as connection:
        connection.execute(sa.text(
            'ALTER TABLE routine_scheduled_quota_reservations '
            'ALTER COLUMN vendor_key TYPE varchar(254)'
        ))
    with pytest.raises(SchemaAdoptionRefused):
        adopt(database)
    assert revision(database) == ["0031"]


def test_predecessor_check_drift_refuses_before_bookkeeping(database):
    with database.begin() as connection:
        connection.execute(sa.text(
            "ALTER TABLE pinterest_autonomous_execution_runs "
            "DROP CONSTRAINT ck_pinterest_auto_exec_status"
        ))
    with pytest.raises(SchemaAdoptionRefused, match="frozen lineage contract"):
        adopt(database)
    assert revision(database) == ["0030"]


def test_shadow_search_path_cannot_redirect_bookkeeping_or_guard(database, monkeypatch):
    with database.begin() as connection:
        connection.execute(sa.text("CREATE SCHEMA shadow"))
        connection.execute(sa.text(
            "CREATE TABLE shadow.alembic_version (version_num varchar(32) NOT NULL)"
        ))
        connection.execute(sa.text(
            "INSERT INTO shadow.alembic_version VALUES ('0029')"
        ))
    with database.begin() as connection:
        connection.execute(sa.text("SET LOCAL search_path TO shadow, public"))
        assert adopt_managed_preapplied_0031(connection)
        assert connection.scalar(sa.text(
            "SELECT version_num FROM shadow.alembic_version"
        )) == "0029"
    assert revision(database) == ["0031"]

    @contextmanager
    def shadowed_connection():
        with database.connect() as connection:
            connection.execute(sa.text("SET LOCAL search_path TO shadow, public"))
            yield connection
    monkeypatch.setattr(
        schema_canonicality_guard, "engine",
        type("ShadowEngine", (), {"connect": staticmethod(shadowed_connection)})(),
    )
    # Historical adoption still reaches 0031, but cannot bypass the new 0032
    # production guard, including through a shadow bookkeeping table.
    with pytest.raises(SchemaAdoptionRefused, match="Alembic revision 0033"):
        schema_canonicality_guard.main()


def test_temporary_table_cannot_shadow_locked_public_bookkeeping(database):
    with database.begin() as connection:
        connection.execute(sa.text(
            "CREATE TEMP TABLE alembic_version (version_num varchar(32) NOT NULL)"
        ))
        connection.execute(sa.text(
            "INSERT INTO alembic_version VALUES ('0029')"
        ))
        assert adopt_managed_preapplied_0031(connection) is True
        assert connection.scalar(sa.text(
            "SELECT version_num FROM pg_temp.alembic_version"
        )) == "0029"
        assert connection.scalar(sa.text(
            "SELECT version_num FROM public.alembic_version"
        )) == "0031"
    assert revision(database) == ["0031"]


def test_startup_adopts_0031_but_new_guard_blocks_app_until_reviewed_0032(database, monkeypatch):
    script = BACKEND.parent / "scripts" / "start_production.py"
    spec = importlib.util.spec_from_file_location("task60_production_startup", script)
    assert spec and spec.loader
    startup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(startup)
    monkeypatch.setattr(managed_schema_adoption, "engine", database)
    monkeypatch.setattr(schema_canonicality_guard, "engine", database)
    events = []

    def run_adoption():
        events.append("adoption")
        assert managed_schema_adoption.main() == 0

    def run_guard():
        events.append("guard")
        assert schema_canonicality_guard.main() == 0

    monkeypatch.setattr(startup, "run_managed_schema_adoption", run_adoption)
    monkeypatch.setattr(startup, "run_schema_canonicality_guard", run_guard)
    monkeypatch.setattr(startup, "start_backend", lambda: events.append("backend") or object())
    monkeypatch.setattr(startup, "wait_for_backend_ready", lambda *_a, **_k: events.append("ready"))
    monkeypatch.setattr(startup, "start_frontend", lambda: events.append("frontend") or object())
    monkeypatch.setattr(startup, "supervise", lambda *_a: 0)
    monkeypatch.setattr(startup, "terminate_process", lambda _process: None)
    with pytest.raises(SchemaAdoptionRefused, match="0033 adoption lock or table availability failed"):
        startup.run()
    assert events == ["adoption"]
    assert revision(database) == ["0030"]


def test_refused_adoption_starts_no_guard_or_app(database, monkeypatch):
    script = BACKEND.parent / "scripts" / "start_production.py"
    spec = importlib.util.spec_from_file_location("task60_failed_startup", script)
    assert spec and spec.loader
    startup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(startup)
    monkeypatch.setattr(startup, "run_managed_schema_adoption", lambda: (
        _ for _ in ()
    ).throw(startup.StartupError("adoption refused")))
    for method in ("run_schema_canonicality_guard", "start_backend", "start_frontend"):
        monkeypatch.setattr(startup, method, lambda: pytest.fail(
            "no guard or application may start after adoption refusal"
        ))
    assert startup.run() == 1
    assert revision(database) == ["0030"]