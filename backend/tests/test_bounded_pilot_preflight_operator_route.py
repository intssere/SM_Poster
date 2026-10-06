"""Task 61.51: authenticated read-only bounded-preflight operator route."""
from __future__ import annotations

import inspect

import pytest
from fastapi.testclient import TestClient

from app.core import auth
from app.core.config import get_settings
from app.api.routes import bounded_pilot_preflight as route


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


def _success():
    return {
        "success": True,
        "mode": "READ_ONLY_FIVE_PIN_BOUNDED_PREFLIGHT",
        "terminal_stage": "COMPLETE",
        "database_transactions": 1,
        "database_writes": 0,
        "provider_calls": 0,
        "provider_reads": 0,
        "provider_writes": 0,
        "object_storage_reads": 0,
        "object_storage_writes": 0,
        "ai_calls": 0,
        "publication_creations": 0,
        "creative_creations": 0,
        "approval_creations": 0,
        "permit_creations": 0,
        "batch_creations": 0,
        "scheduler_activations": 0,
        "worker_activations": 0,
        "autonomy_activations": 0,
        "publishing_admission": "NOT_GRANTED",
        "bounded_preflight_certification": "PASS",
        "database_revision": "0034",
        "schema_canonicality": "PASS",
        "routine_state": "PAUSED",
        "publish_unknown_count": 0,
        "conflicting_nonterminal_batch_count": 0,
        "plan_id": "plan",
        "plan_fingerprint": "1" * 64,
        "candidate_count": 5,
        "preflight_fingerprint": "2" * 64,
        "candidates": [
            {
                "item_id": f"item-{i}",
                "item_fingerprint": f"{100 + i:064x}",
                "candidate_fingerprint": f"{200 + i:064x}",
                "candidate_identity_fingerprint": f"{300 + i:064x}",
                "product_id": f"product-{i}",
                "local_board_id": "local-board",
                "pinterest_board_record_id": "provider-board",
                "external_board_id": "external-board",
                "content_angle_id": "angle",
                "planned_date": f"2026-10-{10 + i:02d}",
                "slot_index": i,
            }
            for i in range(5)
        ],
    }


def _login(client):
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "secret"},
    )
    assert response.status_code == 200


def test_operator_preflight_requires_real_authenticated_admin(client, monkeypatch):
    called = []
    monkeypatch.setattr(route, "run_bounded_preflight", lambda: called.append(1))

    response = client.get(
        "/api/internal/operations/bounded-pilot-preflight",
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 401
    assert called == []


def test_operator_preflight_requires_allowed_origin_even_for_get(client, monkeypatch):
    _login(client)
    called = []
    monkeypatch.setattr(route, "run_bounded_preflight", lambda: called.append(1))

    response = client.get("/api/internal/operations/bounded-pilot-preflight")
    assert response.status_code == 403
    assert response.json() == {"detail": {"code": "ORIGIN_REQUIRED"}}
    assert response.headers["cache-control"] == "no-store"
    assert called == []


def test_operator_preflight_returns_exact_sanitized_dossier_once(client, monkeypatch):
    _login(client)
    calls = []

    def fake():
        calls.append(1)
        return _success()

    monkeypatch.setattr(route, "run_bounded_preflight", fake)
    response = client.get(
        "/api/internal/operations/bounded-pilot-preflight",
        headers={"Origin": ORIGIN},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == _success()
    assert calls == [1]
    assert response.json()["candidate_count"] == 5
    assert len(response.json()["candidates"]) == 5
    assert response.json()["publishing_admission"] == "NOT_GRANTED"


@pytest.mark.parametrize(
    "request_kwargs",
    [
        {"params": {"plan_id": "forbidden"}},
        {"content": b'{"plan_id":"forbidden"}'},
    ],
)
def test_operator_preflight_rejects_all_request_inputs_before_service(
    client, monkeypatch, request_kwargs
):
    _login(client)
    calls = []
    monkeypatch.setattr(route, "run_bounded_preflight", lambda: calls.append(1))

    response = client.request(
        "GET",
        "/api/internal/operations/bounded-pilot-preflight",
        headers={"Origin": ORIGIN},
        **request_kwargs,
    )
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["code"] == "REQUEST_INPUTS_PROHIBITED"
    assert response.json()["publishing_admission"] == "NOT_GRANTED"
    assert calls == []


def test_operator_preflight_maps_certification_refusal_to_409(client, monkeypatch):
    _login(client)
    refused = {
        "success": False,
        "mode": "READ_ONLY_FIVE_PIN_BOUNDED_PREFLIGHT",
        "terminal_stage": "READ_ONLY_DATABASE",
        "database_transactions": 1,
        "database_writes": 0,
        "provider_calls": 0,
        "bounded_preflight_certification": "NOT_GRANTED",
        "publishing_admission": "NOT_GRANTED",
        "candidates": [],
    }
    monkeypatch.setattr(route, "run_bounded_preflight", lambda: refused)

    response = client.get(
        "/api/internal/operations/bounded-pilot-preflight",
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 409
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == refused


def test_operator_preflight_sanitizes_unexpected_exception(client, monkeypatch):
    _login(client)

    def boom():
        raise RuntimeError("PRIVATE_DATABASE_URL_AND_SECRET")

    monkeypatch.setattr(route, "run_bounded_preflight", boom)
    response = client.get(
        "/api/internal/operations/bounded-pilot-preflight",
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 409
    assert response.headers["cache-control"] == "no-store"
    assert "PRIVATE_DATABASE_URL_AND_SECRET" not in response.text
    assert response.json() == {
        "success": False,
        "mode": "READ_ONLY_FIVE_PIN_BOUNDED_PREFLIGHT",
        "terminal_stage": "UNEXPECTED",
        "bounded_preflight_certification": "NOT_GRANTED",
        "publishing_admission": "NOT_GRANTED",
    }


def test_operator_route_has_no_mutation_provider_or_db_session_dependency():
    source = inspect.getsource(route)
    for forbidden in (
        "get_db",
        "Session",
        "BufferGateway",
        "PinterestClient",
        "create_pinterest_post",
        "prepare_batch",
        "create_batch",
        "create_permit",
        "execute_autonomous_item",
        "object_storage",
    ):
        assert forbidden not in source
    assert "run_bounded_preflight" in source
    assert "@router.get" in source
    assert "@router.post" not in source
    assert "@router.put" not in source
    assert "@router.patch" not in source
    assert "@router.delete" not in source
