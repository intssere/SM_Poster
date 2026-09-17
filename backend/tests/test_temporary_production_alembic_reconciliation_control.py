from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.services import production_alembic_reconciliation_control as control


ATTESTATION = {
    "source": "replit_pending_schema_diff",
    "repl_id": control.EXPECTED_REPL_ID,
    "database_scope": "production",
    "database_identity_sha256": "d" * 64,
    "checked_at": "2026-09-17T12:00:00Z",
    "pending_statements": 0,
    "structural_data_loss": False,
    "potential_incompatibility": False,
    "warnings": [],
}


def _attestation_sha256() -> str:
    raw = json.dumps(ATTESTATION, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _configure_production(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("REPLIT_DEPLOYMENT", "1")
    monkeypatch.setenv("REPL_ID", control.EXPECTED_REPL_ID)
    monkeypatch.setenv("ALEMBIC_RECONCILIATION_SECRET", "s" * 48)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@db.example/neondb")
    monkeypatch.setenv("AUTH_ALLOWED_ORIGINS", "https://Diamondshelf.replit.app")
    get_settings.cache_clear()


class _Result:
    def __init__(self, *, one=None, rows=None):
        self._one = one
        self._rows = rows

    def one(self):
        return self._one

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self, *, revision="0017", system_identifier=control.EXPECTED_DATABASE_SYSTEM_IDENTIFIER):
        self.revision = revision
        self.system_identifier = system_identifier

    def execute(self, statement):
        sql = str(statement)
        if "pg_control_system" in sql:
            return _Result(one=(self.system_identifier, control.EXPECTED_DATABASE_NAME))
        if "SELECT version_num FROM alembic_version FOR UPDATE" in sql:
            return _Result(rows=[(self.revision,)])
        raise AssertionError(f"unexpected SQL in temporary control test: {sql}")


class _Engine:
    def __init__(self, connection):
        self.connection = connection
        self.disposed = False

    @contextmanager
    def begin(self):
        yield self.connection

    def dispose(self):
        self.disposed = True


def _fake_reconciler(*, reconciled=True):
    calls = {"reconcile": 0, "attestation": 0}

    def load_attestation(path, **kwargs):
        calls["attestation"] += 1
        assert path.read_bytes()
        assert kwargs["expected_repl_id"] == control.EXPECTED_REPL_ID
        assert kwargs["expected_database_identity_sha256"] == "d" * 64
        return ATTESTATION

    def reconcile(_connection):
        calls["reconcile"] += 1
        if not reconciled:
            return SimpleNamespace(
                mutated=False,
                status="already_0018",
                revision_before="0018",
                revision_after="0018",
            )
        return SimpleNamespace(
            mutated=True,
            status="reconciled",
            revision_before="0017",
            revision_after="0018",
        )

    return SimpleNamespace(
        _sqlalchemy_database_url=lambda value: value,
        database_identity_sha256=lambda _value: "d" * 64,
        load_replit_schema_diff_attestation=load_attestation,
        reconcile_revision=reconcile,
        calls=calls,
    )


def test_static_runtime_and_secret_guards_fail_closed(monkeypatch):
    _configure_production(monkeypatch)
    control._validate_runtime_and_secret(
        supplied_secret="s" * 48,
        confirmation=control.CONFIRMATION,
    )

    with pytest.raises(control.ReconciliationControlRefused, match="authorization secret differs"):
        control._validate_runtime_and_secret(
            supplied_secret="wrong",
            confirmation=control.CONFIRMATION,
        )

    monkeypatch.setenv("REPL_ID", "wrong-app")
    with pytest.raises(control.ReconciliationControlRefused, match="app id differs"):
        control._validate_runtime_and_secret(
            supplied_secret="s" * 48,
            confirmation=control.CONFIRMATION,
        )


def test_control_executes_only_exact_guarded_reconciliation(monkeypatch):
    _configure_production(monkeypatch)
    fake_reconciler = _fake_reconciler()
    engine = _Engine(_Connection())
    monkeypatch.setattr(control, "_load_canonical_reconciler", lambda: fake_reconciler)
    monkeypatch.setattr(control.sa, "create_engine", lambda *args, **kwargs: engine)

    result = control.execute_temporary_reconciliation(
        supplied_secret="s" * 48,
        confirmation=control.CONFIRMATION,
        attestation=ATTESTATION,
        attestation_sha256=_attestation_sha256(),
    )

    assert result == {
        "status": "reconciled",
        "revision_before": "0017",
        "revision_after": "0018",
        "mutated": True,
        "control": "temporary-production-alembic-reconciliation",
    }
    assert fake_reconciler.calls == {"reconcile": 1, "attestation": 1}
    assert engine.disposed is True


def test_control_self_invalidates_when_revision_is_not_0017(monkeypatch):
    _configure_production(monkeypatch)
    fake_reconciler = _fake_reconciler()
    engine = _Engine(_Connection(revision="0018"))
    monkeypatch.setattr(control, "_load_canonical_reconciler", lambda: fake_reconciler)
    monkeypatch.setattr(control.sa, "create_engine", lambda *args, **kwargs: engine)

    with pytest.raises(control.ReconciliationControlRefused, match="exactly one row at 0017"):
        control.execute_temporary_reconciliation(
            supplied_secret="s" * 48,
            confirmation=control.CONFIRMATION,
            attestation=ATTESTATION,
            attestation_sha256=_attestation_sha256(),
        )
    assert fake_reconciler.calls["reconcile"] == 0


def test_control_rejects_wrong_production_database_cluster(monkeypatch):
    _configure_production(monkeypatch)
    fake_reconciler = _fake_reconciler()
    engine = _Engine(_Connection(system_identifier="wrong-cluster"))
    monkeypatch.setattr(control, "_load_canonical_reconciler", lambda: fake_reconciler)
    monkeypatch.setattr(control.sa, "create_engine", lambda *args, **kwargs: engine)

    with pytest.raises(control.ReconciliationControlRefused, match="cluster identity differs"):
        control.execute_temporary_reconciliation(
            supplied_secret="s" * 48,
            confirmation=control.CONFIRMATION,
            attestation=ATTESTATION,
            attestation_sha256=_attestation_sha256(),
        )
    assert fake_reconciler.calls["reconcile"] == 0


def test_control_rejects_attestation_digest_mismatch(monkeypatch):
    _configure_production(monkeypatch)
    monkeypatch.setattr(control, "_load_canonical_reconciler", lambda: _fake_reconciler())

    with pytest.raises(control.ReconciliationControlRefused, match="attestation SHA-256 does not match"):
        control.execute_temporary_reconciliation(
            supplied_secret="s" * 48,
            confirmation=control.CONFIRMATION,
            attestation=ATTESTATION,
            attestation_sha256="0" * 64,
        )


def test_canonical_reconciler_source_identity_is_pinned(monkeypatch, tmp_path):
    fake = tmp_path / "reconciler.py"
    fake.write_text("print('not canonical')\n")
    monkeypatch.setattr(control, "RECONCILER_PATH", fake)

    with pytest.raises(control.ReconciliationControlRefused, match="source identity differs"):
        control._load_canonical_reconciler()


def test_hidden_route_is_public_only_at_secret_gated_boundary(monkeypatch):
    _configure_production(monkeypatch)
    from app.api.routes import production_alembic_reconciliation as route
    from app.main import app

    monkeypatch.setattr(
        route,
        "execute_temporary_reconciliation",
        lambda **kwargs: {
            "status": "reconciled",
            "revision_before": "0017",
            "revision_after": "0018",
            "mutated": True,
            "control": "temporary-production-alembic-reconciliation",
        },
    )
    client = TestClient(app)
    payload = {
        "authorization_secret": "s" * 48,
        "confirmation": control.CONFIRMATION,
        "attestation": ATTESTATION,
        "attestation_sha256": _attestation_sha256(),
    }
    path = "/api/maintenance/production-alembic-0017-0018/reconcile"

    assert client.post(path, json=payload).status_code == 403
    response = client.post(
        path,
        json=payload,
        headers={"Origin": "https://Diamondshelf.replit.app"},
    )
    assert response.status_code == 200
    assert path not in client.get("/openapi.json").json()["paths"]
