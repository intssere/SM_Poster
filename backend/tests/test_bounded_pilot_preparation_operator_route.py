"""Task 61.52: bounded preparation operator route regressions."""
from __future__ import annotations

import inspect

import pytest
from fastapi.testclient import TestClient

from app.api.routes import bounded_pilot_preparation as route
from app.core import auth
from app.core.config import get_settings
from app.services.bounded_pilot_preparation_operator import PREPARATION_CONTRACT


ORIGIN = "http://localhost:5000"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("APP_SECRET_KEY", "s" * 48)
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", auth.hash_password("secret"))
    monkeypatch.setenv("AUTH_ALLOWED_ORIGINS", ORIGIN)
    monkeypatch.delenv("REPLIT_DEPLOYMENT", raising=False)
    monkeypatch.delenv("REPLIT_DEV_DOMAIN", raising=False)
    get_settings.cache_clear()
    from app.main import app
    return TestClient(app)


def _candidate(index: int):
    raw = {
        "item_id": f"item-{index}",
        "item_fingerprint": f"{100 + index:064x}",
        "candidate_fingerprint": f"{200 + index:064x}",
        "product_id": f"product-{index}",
        "local_board_id": "local-board",
        "pinterest_board_record_id": "provider-board",
        "external_board_id": "external-board",
        "content_angle_id": "angle",
        "planned_date": f"2026-10-{10 + index:02d}",
        "slot_index": index,
    }
    from app.services import routine_bounded_preparation as preparation
    return {**raw, "candidate_identity_fingerprint": preparation._digest(raw)}


def _payload():
    from app.services import routine_bounded_preparation as preparation
    candidates = [_candidate(i) for i in range(5)]
    preflight = {
        "contract": preparation.PREFLIGHT_CONTRACT,
        "database_revision": "0034",
        "month_start": "2026-10-01",
        "current_date": "2026-10-06",
        "plan_id": "plan",
        "plan_fingerprint": "1" * 64,
        "candidates": candidates,
    }
    preflight["preflight_fingerprint"] = preparation._digest(preflight)
    return {
        "confirmed": True,
        "confirmation_text_version": PREPARATION_CONTRACT,
        "preflight": preflight,
    }


def _login(client):
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "secret"},
    )
    assert response.status_code == 200


def test_preparation_requires_authenticated_real_admin(client, monkeypatch):
    calls = []
    monkeypatch.setattr(route, "prepare_certified_batch", lambda *a, **k: calls.append(1))
    response = client.post(
        "/api/internal/operations/bounded-pilot-preparation",
        headers={"Origin": ORIGIN},
        json=_payload(),
    )
    assert response.status_code == 401
    assert calls == []


def test_preparation_requires_exact_confirmation(client, monkeypatch):
    _login(client)
    calls = []
    monkeypatch.setattr(route, "prepare_certified_batch", lambda *a, **k: calls.append(1))
    payload = _payload()
    payload["confirmed"] = False
    response = client.post(
        "/api/internal/operations/bounded-pilot-preparation",
        headers={"Origin": ORIGIN},
        json=payload,
    )
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["code"] == "EXPLICIT_PREPARATION_CONFIRMATION_REQUIRED"
    assert calls == []


def test_preparation_rejects_query_inputs_before_service(client, monkeypatch):
    _login(client)
    calls = []
    monkeypatch.setattr(route, "prepare_certified_batch", lambda *a, **k: calls.append(1))
    response = client.post(
        "/api/internal/operations/bounded-pilot-preparation?batch_id=forbidden",
        headers={"Origin": ORIGIN},
        json=_payload(),
    )
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["code"] == "REQUEST_QUERY_INPUTS_PROHIBITED"
    assert calls == []


def test_preparation_delegates_exact_receipt_and_returns_no_store(client, monkeypatch):
    _login(client)
    calls = []
    expected = {
        "success": True,
        "contract": PREPARATION_CONTRACT,
        "status": "READY",
        "batch_id": "batch",
        "batch_manifest_sha256": "2" * 64,
        "preflight_fingerprint": _payload()["preflight"]["preflight_fingerprint"],
        "plan_id": "plan",
        "plan_fingerprint": "1" * 64,
        "candidate_count": 5,
        "entries": [],
        "provider_calls": 0,
        "buffer_calls": 0,
        "pinterest_calls": 0,
        "oauth_calls": 0,
        "ai_calls": 0,
        "publishing_admission": "NOT_GRANTED",
        "idempotent": False,
    }

    def prepare(db, *, settings, actor, receipt, renderer=None):
        calls.append((actor, receipt, renderer))
        return expected

    monkeypatch.setattr(route, "prepare_certified_batch", prepare)
    response = client.post(
        "/api/internal/operations/bounded-pilot-preparation",
        headers={"Origin": ORIGIN},
        json=_payload(),
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == expected
    assert calls == [("admin", _payload()["preflight"], None)]


def test_preparation_maps_bounded_failure_without_secret_text(client, monkeypatch):
    _login(client)

    def fail(*args, **kwargs):
        raise route.BoundedPreparationOperatorError("BOUNDED_PREPARATION_PLAN_DRIFT")

    monkeypatch.setattr(route, "prepare_certified_batch", fail)
    response = client.post(
        "/api/internal/operations/bounded-pilot-preparation",
        headers={"Origin": ORIGIN},
        json=_payload(),
    )
    assert response.status_code == 409
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "success": False,
        "contract": PREPARATION_CONTRACT,
        "code": "BOUNDED_PREPARATION_PLAN_DRIFT",
        "publishing_admission": "NOT_GRANTED",
    }


def test_preparation_sanitizes_unexpected_exception(client, monkeypatch):
    _login(client)

    def fail(*args, **kwargs):
        raise RuntimeError("PRIVATE_DATABASE_URL_AND_TOKEN")

    monkeypatch.setattr(route, "prepare_certified_batch", fail)
    response = client.post(
        "/api/internal/operations/bounded-pilot-preparation",
        headers={"Origin": ORIGIN},
        json=_payload(),
    )
    assert response.status_code == 500
    assert "PRIVATE_DATABASE_URL_AND_TOKEN" not in response.text
    assert response.json()["code"] == "BOUNDED_PREPARATION_UNEXPECTED_ERROR"
    assert response.json()["publishing_admission"] == "NOT_GRANTED"


def test_route_has_no_provider_or_arbitrary_batch_control_dependency():
    from app.services import bounded_pilot_preparation_operator as operator

    source = inspect.getsource(route)
    service_source = inspect.getsource(operator)
    for forbidden in (
        "BufferGateway",
        "PinterestClient",
        "create_pinterest_post",
        "run_bounded_batch_once",
        "set_control",
    ):
        assert forbidden not in source
        assert forbidden not in service_source
    assert "batch_id:" not in source
    assert "@router.post" in source
    assert "require_real_admin" in source
    assert "prepare_certified_batch" in source
