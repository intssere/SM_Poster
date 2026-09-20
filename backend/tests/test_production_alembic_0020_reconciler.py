from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa


SCRIPT_PATH = (
    Path(__file__).parents[1]
    / "scripts"
    / "reconcile_production_alembic_revision_0020.py"
)
spec = importlib.util.spec_from_file_location(
    "production_alembic_0020_reconciler", SCRIPT_PATH
)
assert spec and spec.loader
reconciler = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = reconciler
spec.loader.exec_module(reconciler)


class FakeRowsResult:
    def __init__(self, rows=None, rowcount=-1):
        self._rows = list(rows or [])
        self.rowcount = rowcount

    def fetchall(self):
        return list(self._rows)


class FakeConnection:
    def __init__(self, revision="0019", *, dialect="postgresql", cas_rowcount=1):
        self.revision = revision
        self.dialect = SimpleNamespace(name=dialect)
        self.cas_rowcount = cas_rowcount
        self.calls = []

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        self.calls.append((sql, dict(params or {})))
        if sql == "SELECT version_num FROM alembic_version FOR UPDATE":
            return FakeRowsResult(rows=[(self.revision,)])
        if sql.startswith("UPDATE alembic_version SET version_num"):
            if self.cas_rowcount == 1:
                assert params == {"target": "0020", "current": "0019"}
                self.revision = "0020"
            return FakeRowsResult(rowcount=self.cas_rowcount)
        if sql == "SELECT version_num FROM alembic_version":
            return FakeRowsResult(rows=[(self.revision,)])
        raise AssertionError(f"unexpected SQL: {sql}")


def _attestation_payload(now, database_identity="d" * 64):
    return {
        "source": reconciler.SOURCE,
        "repl_id": "repl-123",
        "database_scope": "production",
        "database_identity_sha256": database_identity,
        "checked_at": now.isoformat(),
        "pending_statements": 0,
        "structural_data_loss": False,
        "potential_incompatibility": False,
        "warnings": [],
    }


def _write_attestation(tmp_path, payload):
    raw = json.dumps(payload, sort_keys=True).encode("utf-8")
    path = tmp_path / "attestation.json"
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


def test_valid_zero_diff_attestation_is_accepted(tmp_path):
    now = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)
    payload = _attestation_payload(now)
    path, digest = _write_attestation(tmp_path, payload)
    loaded = reconciler.load_replit_schema_diff_attestation(
        path,
        expected_sha256=digest,
        expected_repl_id="repl-123",
        expected_database_identity_sha256="d" * 64,
        now=now,
    )
    assert loaded == payload


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("pending_statements", 1, "pending schema diff"),
        ("structural_data_loss", True, "structural data loss"),
        ("potential_incompatibility", True, "potential incompatibility"),
        ("warnings", ["warning"], "contains warnings"),
        ("database_scope", "development", "scope is not production"),
    ],
)
def test_attestation_fails_closed_on_unsafe_state(tmp_path, field, value, message):
    now = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)
    payload = _attestation_payload(now)
    payload[field] = value
    path, digest = _write_attestation(tmp_path, payload)
    with pytest.raises(reconciler.ReconciliationRefused, match=message):
        reconciler.load_replit_schema_diff_attestation(
            path,
            expected_sha256=digest,
            expected_repl_id="repl-123",
            expected_database_identity_sha256="d" * 64,
            now=now,
        )


def test_attestation_rejects_stale_or_tampered_evidence(tmp_path):
    now = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)
    payload = _attestation_payload(now - timedelta(seconds=301))
    path, digest = _write_attestation(tmp_path, payload)
    with pytest.raises(reconciler.ReconciliationRefused, match="stale"):
        reconciler.load_replit_schema_diff_attestation(
            path,
            expected_sha256=digest,
            expected_repl_id="repl-123",
            expected_database_identity_sha256="d" * 64,
            now=now,
        )
    with pytest.raises(reconciler.ReconciliationRefused, match="SHA-256"):
        reconciler.load_replit_schema_diff_attestation(
            path,
            expected_sha256="0" * 64,
            expected_repl_id="repl-123",
            expected_database_identity_sha256="d" * 64,
            now=now,
        )


def test_0019_exact_schema_reconciles_only_version_row(monkeypatch):
    connection = FakeConnection("0019")
    validated = []
    monkeypatch.setattr(
        reconciler,
        "_validate_final_schema",
        lambda bind: validated.append(("schema", bind)),
    )
    monkeypatch.setattr(
        reconciler,
        "_validate_empty_task50_tables",
        lambda bind: validated.append(("empty", bind)),
    )

    result = reconciler.reconcile_revision(connection)

    assert result == reconciler.ReconciliationResult(
        status="reconciled",
        revision_before="0019",
        revision_after="0020",
        mutated=True,
    )
    assert validated == [("schema", connection), ("empty", connection)]
    assert connection.revision == "0020"
    assert [call[0] for call in connection.calls] == [
        "SELECT version_num FROM alembic_version FOR UPDATE",
        "UPDATE alembic_version SET version_num = :target WHERE version_num = :current",
        "SELECT version_num FROM alembic_version",
    ]
    assert all(
        not sql.upper().startswith(("CREATE ", "ALTER ", "DROP ", "TRUNCATE ", "INSERT ", "DELETE "))
        for sql, _ in connection.calls
    )


def test_already_0020_is_idempotent_but_still_validates(monkeypatch):
    connection = FakeConnection("0020")
    validated = []
    monkeypatch.setattr(
        reconciler, "_validate_final_schema", lambda bind: validated.append("schema")
    )
    monkeypatch.setattr(
        reconciler, "_validate_empty_task50_tables", lambda bind: validated.append("empty")
    )
    result = reconciler.reconcile_revision(connection)
    assert result.status == "already_0020"
    assert result.mutated is False
    assert validated == ["schema", "empty"]
    assert len(connection.calls) == 1


def test_unexpected_revision_refuses_before_validation(monkeypatch):
    connection = FakeConnection("0018")
    called = []
    monkeypatch.setattr(
        reconciler, "_validate_final_schema", lambda bind: called.append(bind)
    )
    monkeypatch.setattr(
        reconciler, "_validate_empty_task50_tables", lambda bind: called.append(bind)
    )
    with pytest.raises(reconciler.ReconciliationRefused, match="unexpected Alembic revision"):
        reconciler.reconcile_revision(connection)
    assert called == []
    assert not any(sql.startswith("UPDATE ") for sql, _ in connection.calls)


def test_schema_or_nonempty_table_refuses_before_update(monkeypatch):
    connection = FakeConnection("0019")
    monkeypatch.setattr(
        reconciler,
        "_validate_final_schema",
        lambda _: (_ for _ in ()).throw(reconciler.ReconciliationRefused("schema drift")),
    )
    with pytest.raises(reconciler.ReconciliationRefused, match="schema drift"):
        reconciler.reconcile_revision(connection)
    assert not any(sql.startswith("UPDATE ") for sql, _ in connection.calls)

    connection = FakeConnection("0019")
    monkeypatch.setattr(reconciler, "_validate_final_schema", lambda _: None)
    monkeypatch.setattr(
        reconciler,
        "_validate_empty_task50_tables",
        lambda _: (_ for _ in ()).throw(reconciler.ReconciliationRefused("not empty")),
    )
    with pytest.raises(reconciler.ReconciliationRefused, match="not empty"):
        reconciler.reconcile_revision(connection)
    assert not any(sql.startswith("UPDATE ") for sql, _ in connection.calls)


def test_non_postgresql_refuses_without_sql():
    connection = FakeConnection("0019", dialect="sqlite")
    with pytest.raises(reconciler.ReconciliationRefused, match="not PostgreSQL"):
        reconciler.reconcile_revision(connection)
    assert connection.calls == []


def test_compare_and_set_race_refuses(monkeypatch):
    connection = FakeConnection("0019", cas_rowcount=0)
    monkeypatch.setattr(reconciler, "_validate_final_schema", lambda _: None)
    monkeypatch.setattr(reconciler, "_validate_empty_task50_tables", lambda _: None)
    with pytest.raises(reconciler.ReconciliationRefused, match="compare-and-set"):
        reconciler.reconcile_revision(connection)
    assert connection.revision == "0019"


POSTGRES_URL = os.getenv("TASK501_POSTGRES_URL")


@pytest.mark.skipif(not POSTGRES_URL, reason="TASK501_POSTGRES_URL not configured")
def test_real_postgres_structural_0020_bookkeeping_0019_reconciles_only_revision():
    backend_dir = Path(__file__).parents[1]
    env = dict(os.environ)
    env["DATABASE_URL"] = POSTGRES_URL
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=backend_dir,
        env=env,
        check=True,
    )

    engine = sa.create_engine(POSTGRES_URL, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            assert connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one() == "0020"
            assert connection.execute(sa.text("SELECT count(*) FROM pinterest_portfolio_plans")).scalar_one() == 0
            assert connection.execute(sa.text("SELECT count(*) FROM pinterest_portfolio_plan_items")).scalar_one() == 0
            connection.execute(
                sa.text("UPDATE alembic_version SET version_num='0019' WHERE version_num='0020'")
            )

        with engine.begin() as connection:
            before_tables = {
                name: connection.execute(
                    sa.text(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name=:name"
                    ),
                    {"name": name},
                ).scalar_one()
                for name in reconciler.EXPECTED_TABLES
            }
            result = reconciler.reconcile_revision(connection)
            after_tables = {
                name: connection.execute(
                    sa.text(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name=:name"
                    ),
                    {"name": name},
                ).scalar_one()
                for name in reconciler.EXPECTED_TABLES
            }
            assert result.mutated is True
            assert result.revision_before == "0019"
            assert result.revision_after == "0020"
            assert before_tables == after_tables
            assert connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one() == "0020"
    finally:
        engine.dispose()


@pytest.mark.skipif(not POSTGRES_URL, reason="TASK501_POSTGRES_URL not configured")
def test_real_postgres_reconciler_refuses_nonempty_task50_table():
    engine = sa.create_engine(POSTGRES_URL, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            # Previous integration test leaves the DB at exact 0020.
            store_id = connection.execute(
                sa.text("SELECT id FROM stores ORDER BY id LIMIT 1")
            ).scalar_one_or_none()
            if store_id is None:
                pytest.skip("fixture database contains no store")
            connection.execute(sa.text(
                "INSERT INTO pinterest_portfolio_plans "
                "(id, store_id, month_start, month_end, target_pins, existing_commitments, "
                "planned_active_slots, reserve_slots, policy_version, input_fingerprint, "
                "plan_fingerprint, status, metadata_json) "
                "VALUES ('test-plan', :store_id, DATE '2026-10-01', DATE '2026-10-31', 150, 0, 0, 0, "
                "'test', :input_fp, :plan_fp, 'DRAFT', '{}'::json)"
            ), {"store_id": store_id, "input_fp": "a"*64, "plan_fp": "b"*64})
            with pytest.raises(reconciler.ReconciliationRefused, match="not empty"):
                reconciler.reconcile_revision(connection)
            # Transaction rollback through raised exception leaves no fixture row.
            raise RuntimeError("rollback fixture")
    except RuntimeError as exc:
        if str(exc) != "rollback fixture":
            raise
    finally:
        engine.dispose()
