"""Offline dispatch bridge: real persisted gate, mocked provider boundaries."""
import asyncio
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock

import pytest
from sqlalchemy import select, func, inspect

from app.models.domain import PinPublication, PinterestBoard, PublicationAttempt, PublicationDispatchAuthorization
from app.services import buffer_manual_publication_dispatch as dispatch
from test_buffer_phase3b_execution_gate import _fresh_persisted_case, NOW, SECRET


@pytest.fixture
def candidate(tmp_path):
    factory, engine, pid, aid, bid, settings, evidence = _fresh_persisted_case(tmp_path)
    with factory() as db:
        yield db, factory, db.get(PinPublication, pid), db.get(PublicationDispatchAuthorization, aid), bid, settings, evidence
    engine.dispose()


def snapshot(row):
    return {column.key: getattr(row, column.key) for column in inspect(row).mapper.column_attrs}


@pytest.mark.parametrize("failure", [
    "missing", "flags", "stale", "malformed", "mismatch", "write", "destination", "media",
    "revoked", "expired", "publication-drift", "board-drift", "connection-bound",
])
def test_locked_gate_prevents_all_provider_and_claim_work(candidate, monkeypatch, failure):
    db, factory, publication, auth, bid, settings, evidence = candidate
    if failure == "missing": evidence = None
    if failure == "flags":
        settings = settings.model_copy(update=dict(publishing_enabled=False, buffer_publishing_enabled=False,
                                                   buffer_single_pin_pilot_enabled=False))
    if failure == "stale": evidence = replace(evidence, observed_at=NOW - timedelta(hours=1))
    if failure == "malformed": evidence = replace(evidence, observed_at="invalid")
    if failure == "mismatch": evidence = replace(evidence, media_url="https://example.test/wrong")
    for name, field in [("write", "write_credential_authorized"),
                        ("destination", "provider_destination_live_verified"), ("media", "media_live_fetch_verified")]:
        if failure == name: evidence = replace(evidence, **{field: False})
    # Caller caches valid rows before a second session commits drift.
    board = db.get(PinterestBoard, bid)
    assert board.is_eligible and auth.status == "ACTIVE"
    if failure in {"revoked", "expired", "publication-drift", "board-drift"}:
        with factory() as other:
            if failure == "revoked": other.get(PublicationDispatchAuthorization, auth.id).status = "REVOKED"
            if failure == "expired": other.get(PublicationDispatchAuthorization, auth.id).expires_at = NOW - timedelta(seconds=1)
            if failure == "publication-drift": other.get(PinPublication, publication.id).title_snapshot = None
            if failure == "board-drift": other.get(PinterestBoard, bid).is_eligible = False
            other.commit()
    if failure == "connection-bound":
        connection = db.connection()
        monkeypatch.setattr(db, "get_bind", lambda *args, **kwargs: connection)
    with factory() as other:
        before = snapshot(other.get(PinPublication, publication.id)), snapshot(other.get(PublicationDispatchAuthorization, auth.id))
    spies = {}
    for name in ["BufferGateway", "verify_destination", "atomic_authorized_claim", "build_pinterest_payload"]:
        spies[name] = Mock(side_effect=AssertionError("provider/claim boundary reached"))
        monkeypatch.setattr(dispatch, name, spies[name])
    with pytest.raises(dispatch.ManualDispatchError) as error:
        asyncio.run(dispatch.dispatch_buffer(db, publication, settings=settings, now=NOW, execution_evidence=evidence))
    assert str(error.value) == "BUFFER_EXECUTION_GATE_LOCKED"
    assert SECRET not in str(error.value) and "Authorization" not in str(error.value)
    for spy in spies.values(): spy.assert_not_called()
    with factory() as other:
        assert other.scalar(select(func.count()).select_from(PublicationAttempt)) == 0
        assert snapshot(other.get(PinPublication, publication.id)) == before[0]
        assert snapshot(other.get(PublicationDispatchAuthorization, auth.id)) == before[1]


def test_valid_evidence_gate_precedes_gateway_and_claim(candidate, monkeypatch):
    db, _, publication, auth, _, settings, evidence = candidate
    order = []
    real_gate = dispatch.evaluate_buffer_pilot_execution_readiness
    real_claim = dispatch.atomic_authorized_claim
    def gate(*args, **kwargs):
        result = real_gate(*args, **kwargs)
        assert result["execution_status"] == "FINAL_EXECUTION_READY"
        order.append("gate")
        return result
    class Gateway:
        def __init__(self, settings): order.append("gateway")
        async def create_pinterest_post(self, payload):
            order.append("create")
            raise TimeoutError("mock uncertain outcome")
    async def verify(*args): order.append("verify")
    def claim(*args, **kwargs):
        order.append("claim")
        return real_claim(*args, **kwargs)
    monkeypatch.setattr(dispatch, "evaluate_buffer_pilot_execution_readiness", gate)
    monkeypatch.setattr(dispatch, "BufferGateway", Gateway)
    monkeypatch.setattr(dispatch, "verify_destination", verify)
    monkeypatch.setattr(dispatch, "atomic_authorized_claim", claim)
    asyncio.run(dispatch.dispatch_buffer(db, publication, settings=settings, now=NOW, execution_evidence=evidence))
    assert order == ["gate", "gateway", "verify", "claim", "verify", "create"]
    assert publication.status.value == "PUBLISH_UNKNOWN" and auth.status == "CONSUMED"
    # Restore the real gate to prove the persisted attempt blocks a second call.
    monkeypatch.setattr(dispatch, "evaluate_buffer_pilot_execution_readiness", real_gate)
    with pytest.raises(dispatch.ManualDispatchError):
        asyncio.run(dispatch.dispatch_buffer(db, publication, settings=settings, now=NOW, execution_evidence=evidence))
    assert order.count("create") == 1 and order.count("gateway") == 1
