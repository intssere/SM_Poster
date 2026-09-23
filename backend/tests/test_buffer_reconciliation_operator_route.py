import asyncio
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from app.api.routes import buffer_reconciliation as route


def _payload(*, confirmed=True, version=route.BUFFER_RECONCILIATION_CONFIRMATION_VERSION):
    return route.BufferReconciliationRequest(
        confirmed=confirmed,
        confirmation_text_version=version,
    )


def test_buffer_reconciliation_route_requires_authenticated_operator(monkeypatch):
    calls = []

    async def fake_reconcile(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("service must not run without authentication")

    monkeypatch.setattr(route, "current_user", lambda request: None)
    monkeypatch.setattr(route, "reconcile_buffer", fake_reconcile)

    with pytest.raises(HTTPException) as error:
        asyncio.run(route.reconcile_known_buffer_operation(
            "publication",
            SimpleNamespace(),
            _payload(),
            SimpleNamespace(),
        ))
    assert error.value.status_code == 401
    assert error.value.detail == "Authentication required"
    assert calls == []


@pytest.mark.parametrize(
    "payload,detail",
    [
        (_payload(confirmed=False), "CONFIRMATION_REQUIRED"),
        (_payload(version="WRONG_VERSION"), "INVALID_RECONCILIATION_CONFIRMATION_VERSION"),
    ],
)
def test_buffer_reconciliation_route_requires_exact_operator_confirmation(monkeypatch, payload, detail):
    calls = []

    async def fake_reconcile(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("service must not run with invalid confirmation")

    monkeypatch.setattr(route, "current_user", lambda request: "operator")
    monkeypatch.setattr(route, "reconcile_buffer", fake_reconcile)

    with pytest.raises(HTTPException) as error:
        asyncio.run(route.reconcile_known_buffer_operation(
            "publication",
            SimpleNamespace(),
            payload,
            SimpleNamespace(),
        ))
    assert error.value.status_code == 422
    assert error.value.detail == detail
    assert calls == []


def test_buffer_reconciliation_route_delegates_only_to_exact_reconciliation_service(monkeypatch):
    calls = []
    row = SimpleNamespace(
        id="publication",
        status="PUBLISHED",
        pinterest_pin_id="1093811828279724056",
        published_at=None,
        error_code=None,
    )

    async def fake_reconcile(db, publication_id, *, actor):
        calls.append((db, publication_id, actor))
        return row

    db = SimpleNamespace()
    monkeypatch.setattr(route, "current_user", lambda request: "operator@example.test")
    monkeypatch.setattr(route, "reconcile_buffer", fake_reconcile)

    result = asyncio.run(route.reconcile_known_buffer_operation(
        "publication",
        SimpleNamespace(),
        _payload(),
        db,
    ))

    assert calls == [(db, "publication", "operator@example.test")]
    assert result == {
        "id": "publication",
        "status": "PUBLISHED",
        "pinterest_pin_id": "1093811828279724056",
        "published_at": None,
        "error_code": None,
    }
    # The route module deliberately has no dispatch/create dependency.
    assert not hasattr(route, "dispatch_buffer")
    assert not hasattr(route, "dispatch_publication")


def test_buffer_reconciliation_route_maps_bounded_service_failure_without_retry(monkeypatch):
    calls = []

    async def fake_reconcile(db, publication_id, *, actor):
        calls.append((publication_id, actor))
        raise route.BufferReconciliationError(
            "BUFFER_POST_SNAPSHOT_MISMATCH",
            stage="provider_snapshot",
            field="title",
        )

    monkeypatch.setattr(route, "current_user", lambda request: "operator")
    monkeypatch.setattr(route, "reconcile_buffer", fake_reconcile)

    with pytest.raises(HTTPException) as error:
        asyncio.run(route.reconcile_known_buffer_operation(
            "publication",
            SimpleNamespace(),
            _payload(),
            SimpleNamespace(),
        ))
    assert error.value.status_code == 409
    assert error.value.detail == "BUFFER_POST_SNAPSHOT_MISMATCH"
    assert error.value.headers == {
        "X-Buffer-Reconciliation-Stage": "provider_snapshot",
        "X-Buffer-Reconciliation-Field": "title",
    }
    assert calls == [("publication", "operator")]
