"""Preflight receipts are advisory, not a persisted one-shot authorization.

Production authorization remains the external one-shot authority. Within its
five-minute TTL, a receipt can be reused if the checked state has not changed.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import socket
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select, text

from app.models.domain import PinPublication, PublicationStatus
from app.models.routine_publishing import RoutineDispatchPermit, RoutinePublishingRun
from app.services import routine_certified_canary as certified
from test_routine_canary_fixture import NOW, _scheduler, _settings
from test_routine_certified_canary import canary, _unchanged, _evidence


KEY = "s" * 48


def _issue(db, *, now=NOW, publication_id="canary-source", settings=None):
    return certified.preflight_certified_offline_canary(
        db, publication_id=publication_id, settings=settings or _settings(app_secret_key=KEY),
        now=now, scheduler_snapshot=_scheduler(),
    )


def _execute(db, receipt, *, now=NOW, publication_id="canary-source", version=None, settings=None):
    return certified.certify_one_shot_offline_canary(
        db, publication_id=publication_id, settings=settings or _settings(app_secret_key=KEY),
        preflight_contract_version=version or certified.PREFLIGHT_CONTRACT_VERSION,
        preflight_receipt=receipt, now=now, scheduler_snapshot=_scheduler(),
    )


def _resign(receipt):
    payload = {k: v for k, v in receipt.items() if k != "signature"}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return {**payload, "signature": hmac.new(KEY.encode(), raw, hashlib.sha256).hexdigest()}


def test_preflight_issues_sanitized_five_minute_receipt_and_execution_remains_read_only(canary):
    statements = []
    engine = canary.get_bind()
    def record(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement.strip().split()[0].upper())
    event.listen(engine, "before_cursor_execute", record)
    try:
        receipt = _issue(canary)
        assert set(receipt) == {
            "contract_version", "publication_id", "permit_id", "request_fingerprint",
            "prerequisite_fingerprint", "release_identity", "issued_at", "expires_at", "signature",
        }
        assert receipt["contract_version"] == certified.PREFLIGHT_CONTRACT_VERSION
        assert receipt["publication_id"] == "canary-source"
        assert receipt["permit_id"] == "permit-1"
        assert receipt["request_fingerprint"] == "d" * 64
        assert receipt["expires_at"] == (NOW + timedelta(minutes=5)).isoformat()
        assert len(receipt["prerequisite_fingerprint"]) == len(receipt["signature"]) == 64
        assert "test-only" not in json.dumps(receipt)
        assert statements and set(statements) == {"SELECT"}
        assert not (canary.new or canary.dirty or canary.deleted)
        assert _execute(canary, receipt)["status"] == "SUCCEEDED"
        _unchanged(canary)
        assert not any(sql in {"INSERT", "UPDATE", "DELETE"} for sql in statements)
        # There is no consumption row. The external authorization process must
        # limit executions; the API rechecks all state on each invocation.
        assert _execute(canary, receipt)["external_requests"] == 0
        _unchanged(canary)
    finally:
        event.remove(engine, "before_cursor_execute", record)


@pytest.mark.parametrize(("change", "error"), [
    ("wrong_publication", "CANARY_DUE_IDENTITY_OR_CARDINALITY"),
    ("future", "CANARY_DUE_IDENTITY_OR_CARDINALITY"),
    ("extra_due", "CANARY_DUE_IDENTITY_OR_CARDINALITY"),
    ("missing_permit", "CANARY_ACTIVE_PERMIT_CARDINALITY"),
    ("consumed_permit", "CANARY_PERMIT_INVALID"),
    ("expired_permit", "CANARY_PERMIT_INVALID"),
    ("running", "ROUTINE_WORKER_ALREADY_RUNNING"),
    ("paused", "ROUTINE_CONTROL_NOT_PAUSED"),
    ("gate", "UNSAFE_RUNTIME_GATE_STATE"),
    ("dry_run", "ROUTINE_DRY_RUN_CONFIG_REQUIRED"),
    ("quality", "CANARY_PERMIT_INVALID"),
    ("duplicate", "CANARY_PERMIT_INVALID"),
    ("readiness", "CANARY_PERMIT_INVALID"),
    ("routing", "PERSISTED_PINTEREST_ROUTING_STALE"),
    ("network", "CANARY_NETWORK_OPERATION_ATTEMPTED"),
])
def test_preflight_blocks_the_same_prerequisites(canary, monkeypatch, change, error):
    settings = _settings(app_secret_key=KEY)
    publication_id = "canary-source"
    publication = canary.get(PinPublication, publication_id)
    permit = canary.get(RoutineDispatchPermit, "permit-1")
    if change == "wrong_publication":
        publication_id = "other"
    elif change == "future":
        publication.scheduled_for = NOW + timedelta(minutes=1)
    elif change == "extra_due":
        fields = {col.name: getattr(publication, col.name) for col in PinPublication.__table__.columns}
        fields.update(id="other", publication_fingerprint="e" * 64)
        canary.add(PinPublication(**fields))
    elif change == "missing_permit":
        canary.delete(permit)
    elif change == "consumed_permit":
        permit.consumed_at = NOW
    elif change == "expired_permit":
        permit.expires_at = NOW
    elif change == "running":
        canary.add(RoutinePublishingRun(id="running", mode="DRY_RUN", status="RUNNING", started_at=NOW))
    elif change == "paused":
        from app.models.routine_publishing import RoutinePublishingControl
        canary.get(RoutinePublishingControl, "default").state = "DRY_RUN"
    elif change == "gate":
        settings = _settings(app_secret_key=KEY, buffer_publishing_enabled=True)
    elif change == "dry_run":
        settings = _settings(app_secret_key=KEY, routine_pinterest_dry_run=False)
    elif change in {"quality", "duplicate", "readiness"}:
        monkeypatch.setattr(certified, "validate_permit", lambda *a, **k: {
            "valid": False, "status": f"{change.upper()}_FAILED",
        })
    elif change == "routing":
        monkeypatch.setattr(certified, "build_routine_offline_evidence",
                            lambda *a, **k: (_ for _ in ()).throw(
                                certified.RoutineOfflinePreflightError("PERSISTED_PINTEREST_ROUTING_STALE")
                            ))
    elif change == "network":
        def forbidden_network(*args, **kwargs):
            with socket.socket() as sock:
                sock.connect(("127.0.0.1", 9))
        monkeypatch.setattr(certified, "build_routine_offline_evidence", forbidden_network)
    if change in {"future", "extra_due", "missing_permit", "consumed_permit",
                  "expired_permit", "running", "paused"}:
        canary.commit()
    with pytest.raises(certified.CertifiedCanaryError, match=error):
        _issue(canary, publication_id=publication_id, settings=settings)
    assert canary.scalars(select(RoutineDispatchPermit).where(
        RoutineDispatchPermit.status == "CONSUMED",
    )).all() == []


@pytest.mark.parametrize(("mutation", "error"), [
    ("missing", "CANARY_PREFLIGHT_RECEIPT_REQUIRED"),
    ("extra_field", "CANARY_PREFLIGHT_RECEIPT_REQUIRED"),
    ("wrong_version", "CANARY_PREFLIGHT_RECEIPT_REQUIRED"),
    ("wrong_publication", "CANARY_PREFLIGHT_IDENTITY_MISMATCH"),
    ("wrong_permit", "CANARY_PREFLIGHT_STATE_DRIFT"),
    ("wrong_request", "CANARY_PREFLIGHT_STATE_DRIFT"),
    ("wrong_state", "CANARY_PREFLIGHT_STATE_DRIFT"),
    ("tamper", "CANARY_PREFLIGHT_SIGNATURE_INVALID"),
    ("malformed", "CANARY_PREFLIGHT_RECEIPT_MALFORMED"),
    ("expired", "CANARY_PREFLIGHT_EXPIRED"),
    ("future_issue", "CANARY_PREFLIGHT_EXPIRED"),
])
def test_execution_fails_closed_on_receipt_mismatch(canary, mutation, error):
    receipt = _issue(canary)
    version = certified.PREFLIGHT_CONTRACT_VERSION
    publication_id = "canary-source"
    now = NOW
    if mutation == "missing":
        receipt = None
    elif mutation == "extra_field":
        receipt["extra"] = "unexpected"
    elif mutation == "wrong_version":
        version = "WRONG"
    elif mutation == "wrong_publication":
        publication_id = "other"
    elif mutation == "wrong_permit":
        receipt = _resign({**receipt, "permit_id": "other"})
    elif mutation == "wrong_request":
        receipt = _resign({**receipt, "request_fingerprint": "a" * 64})
    elif mutation == "wrong_state":
        receipt = _resign({**receipt, "prerequisite_fingerprint": "b" * 64})
    elif mutation == "tamper":
        receipt["signature"] = "0" * 64
    elif mutation == "malformed":
        receipt["expires_at"] = "not-a-timestamp"
    elif mutation == "expired":
        now += timedelta(minutes=5)
    elif mutation == "future_issue":
        now -= timedelta(seconds=1)
    with pytest.raises(certified.CertifiedCanaryError, match=error):
        _execute(canary, receipt, version=version, publication_id=publication_id, now=now)
    _unchanged(canary)


def test_state_drift_is_rejected_after_a_valid_preflight(canary):
    receipt = _issue(canary)
    canary.get(PinPublication, "canary-source").title_snapshot = "Changed after preflight"
    canary.commit()
    with pytest.raises(certified.CertifiedCanaryError, match="CANARY_PREFLIGHT_STATE_DRIFT"):
        _execute(canary, receipt)
    _unchanged(canary)


def test_release_identity_change_rejects_receipt_without_executing(canary, monkeypatch):
    def provenance(sha):
        return SimpleNamespace(
            present=True, valid=True, commit_sha=sha, tree_sha="a" * 40,
            release_commit_sha="b" * 40, release_tree_sha="c" * 40,
            topology="canonical_parent_with_checkpoint_overlay",
            overlay_path=".replit", overlay_sha256="d" * 64,
        )
    monkeypatch.setattr(certified, "read_build_provenance", lambda: provenance("1" * 40))
    receipt = _issue(canary)
    monkeypatch.setattr(certified, "read_build_provenance", lambda: provenance("2" * 40))
    with pytest.raises(certified.CertifiedCanaryError, match="CANARY_PREFLIGHT_RELEASE_DRIFT"):
        _execute(canary, receipt)
    _unchanged(canary)


def test_preflight_refuses_missing_signing_key_and_unexpected_writes(canary, monkeypatch):
    with pytest.raises(certified.CertifiedCanaryError, match="CANARY_PREFLIGHT_SIGNING_UNAVAILABLE"):
        _issue(canary, settings=_settings(app_secret_key=""))
    def mutate(db, publication, **kwargs):
        publication.status = PublicationStatus.PUBLISHING
        return _evidence()
    monkeypatch.setattr(certified, "build_routine_offline_evidence", mutate)
    with pytest.raises(certified.CertifiedCanaryError, match="CANARY_UNEXPECTED_DATABASE_WRITE"):
        _issue(canary)
    _unchanged(canary)


def test_priced_catalog_values_have_a_deterministic_canonical_encoding():
    assert certified._canonical(Decimal("19.99")) == "19.99"
    assert certified._canonical(Decimal("24.00")) == "24.00"


@pytest.mark.parametrize("path", ["preflight", "execution"])
@pytest.mark.parametrize("sql", [
    "UPDATE pin_publications SET status = 'PUBLISHING' WHERE id = 'canary-source'",
    "SELECT nextval('nonexistent_canary_sequence')",
    "SELECT 1 INTO TEMP TABLE canary_probe",
])
def test_direct_sql_write_attempt_is_blocked_without_persisting(canary, monkeypatch, path, sql):
    receipt = _issue(canary) if path == "execution" else None
    def forbidden_sql(db, publication, **kwargs):
        db.execute(text(sql))
        return _evidence()
    monkeypatch.setattr(certified, "build_routine_offline_evidence", forbidden_sql)
    with pytest.raises(certified.CertifiedCanaryError, match="CANARY_DATABASE_WRITE_ATTEMPTED"):
        _execute(canary, receipt) if path == "execution" else _issue(canary)
    _unchanged(canary)


def test_receipt_expiring_during_execution_lock_wait_fails_closed(canary, monkeypatch):
    receipt = _issue(canary)
    times = iter([
        NOW + timedelta(minutes=4, seconds=59),
        NOW + timedelta(minutes=4, seconds=59),
        NOW + timedelta(minutes=5),
    ])
    class AdvancingClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(times)
    monkeypatch.setattr(certified, "datetime", AdvancingClock)
    with pytest.raises(certified.CertifiedCanaryError, match="CANARY_PREFLIGHT_EXPIRED"):
        _execute(canary, receipt, now=None)
    _unchanged(canary)