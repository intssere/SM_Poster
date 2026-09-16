from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT_PATH = (
    Path(__file__).parents[1]
    / "scripts"
    / "reconcile_production_alembic_revision.py"
)
spec = importlib.util.spec_from_file_location(
    "production_alembic_bookkeeping_reconciler", SCRIPT_PATH
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
    def __init__(
        self,
        revision="0017",
        *,
        dialect="postgresql",
        rows_override=None,
        cas_rowcount=1,
    ):
        self.revision = revision
        self.dialect = SimpleNamespace(name=dialect)
        self.rows_override = rows_override
        self.cas_rowcount = cas_rowcount
        self.calls = []

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        self.calls.append((sql, dict(params or {})))
        if sql == "SELECT version_num FROM alembic_version FOR UPDATE":
            rows = self.rows_override
            if rows is None:
                rows = [(self.revision,)]
            return FakeRowsResult(rows=rows)
        if sql.startswith("UPDATE alembic_version SET version_num"):
            if self.cas_rowcount == 1:
                assert params == {"target": "0018", "current": "0017"}
                self.revision = "0018"
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


def _load_attestation(tmp_path, payload, now):
    path, digest = _write_attestation(tmp_path, payload)
    return reconciler.load_replit_schema_diff_attestation(
        path,
        expected_sha256=digest,
        expected_repl_id="repl-123",
        expected_database_identity_sha256=payload["database_identity_sha256"],
        now=now,
    )


def test_database_identity_excludes_credentials_and_normalizes_legacy_scheme():
    first = reconciler.database_identity_sha256(
        "postgres://user:secret@db.example.com:5432/social"
    )
    second = reconciler.database_identity_sha256(
        "postgresql://other:other-secret@db.example.com/social"
    )
    assert first == second
    assert first == hashlib.sha256(
        b"postgresql://db.example.com:5432/social"
    ).hexdigest()


def test_valid_zero_diff_attestation_is_accepted(tmp_path):
    now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
    payload = _attestation_payload(now)
    loaded = _load_attestation(tmp_path, payload, now)
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
def test_attestation_fails_closed_on_unsafe_control_plane_state(
    tmp_path, field, value, message
):
    now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
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
    now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
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


def test_0017_exact_schema_reconciles_only_version_row(monkeypatch):
    connection = FakeConnection("0017")
    validations = []
    monkeypatch.setattr(
        reconciler,
        "_validate_final_schema",
        lambda bind: validations.append(bind),
    )

    result = reconciler.reconcile_revision(connection)

    assert result == reconciler.ReconciliationResult(
        status="reconciled",
        revision_before="0017",
        revision_after="0018",
        mutated=True,
    )
    assert validations == [connection]
    assert connection.revision == "0018"
    assert [call[0] for call in connection.calls] == [
        "SELECT version_num FROM alembic_version FOR UPDATE",
        "UPDATE alembic_version SET version_num = :target WHERE version_num = :current",
        "SELECT version_num FROM alembic_version",
    ]
    assert all(
        not sql.upper().startswith(("CREATE ", "ALTER ", "DROP ", "TRUNCATE "))
        for sql, _ in connection.calls
    )


def test_already_0018_is_idempotent_but_still_validates_schema(monkeypatch):
    connection = FakeConnection("0018")
    validations = []
    monkeypatch.setattr(
        reconciler,
        "_validate_final_schema",
        lambda bind: validations.append(bind),
    )

    result = reconciler.reconcile_revision(connection)

    assert result.status == "already_0018"
    assert result.mutated is False
    assert validations == [connection]
    assert len(connection.calls) == 1
    assert connection.calls[0][0].endswith("FOR UPDATE")


def test_structural_drift_refuses_before_any_update(monkeypatch):
    connection = FakeConnection("0017")

    def reject(_):
        raise reconciler.ReconciliationRefused("schema drift")

    monkeypatch.setattr(reconciler, "_validate_final_schema", reject)
    with pytest.raises(reconciler.ReconciliationRefused, match="schema drift"):
        reconciler.reconcile_revision(connection)
    assert not any(sql.startswith("UPDATE ") for sql, _ in connection.calls)
    assert connection.revision == "0017"


def test_unexpected_revision_refuses_before_schema_validation(monkeypatch):
    connection = FakeConnection("0016")
    called = []
    monkeypatch.setattr(
        reconciler,
        "_validate_final_schema",
        lambda bind: called.append(bind),
    )
    with pytest.raises(reconciler.ReconciliationRefused, match="unexpected Alembic revision"):
        reconciler.reconcile_revision(connection)
    assert called == []
    assert not any(sql.startswith("UPDATE ") for sql, _ in connection.calls)


def test_multiple_alembic_rows_refuse_fail_closed(monkeypatch):
    connection = FakeConnection(rows_override=[("0017",), ("0018",)])
    monkeypatch.setattr(reconciler, "_validate_final_schema", lambda _: None)
    with pytest.raises(reconciler.ReconciliationRefused, match="exactly one row"):
        reconciler.reconcile_revision(connection)
    assert not any(sql.startswith("UPDATE ") for sql, _ in connection.calls)


def test_non_postgresql_connection_refuses_without_sql(monkeypatch):
    connection = FakeConnection("0017", dialect="sqlite")
    monkeypatch.setattr(reconciler, "_validate_final_schema", lambda _: None)
    with pytest.raises(reconciler.ReconciliationRefused, match="not PostgreSQL"):
        reconciler.reconcile_revision(connection)
    assert connection.calls == []


def test_compare_and_set_race_refuses(monkeypatch):
    connection = FakeConnection("0017", cas_rowcount=0)
    monkeypatch.setattr(reconciler, "_validate_final_schema", lambda _: None)
    with pytest.raises(reconciler.ReconciliationRefused, match="compare-and-set"):
        reconciler.reconcile_revision(connection)
    assert connection.revision == "0017"


def test_schema_validator_delegates_to_canonical_0018_final_contract(monkeypatch):
    connection = FakeConnection("0017")
    calls = []
    migration = SimpleNamespace(
        _validate_postgresql_contract=lambda bind, mode: calls.append((bind, mode))
    )
    monkeypatch.setattr(reconciler, "_load_migration_0018", lambda: migration)

    reconciler._validate_final_schema(connection)

    assert calls == [(connection, "final")]
