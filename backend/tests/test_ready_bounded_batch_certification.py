"""Task 61.55: READY bounded-batch certification surface regressions."""
from __future__ import annotations

import inspect
import json

import pytest
from fastapi.testclient import TestClient

from app.api.routes import bounded_pilot_ready as route
from app.core import auth
from app.core.config import get_settings
from app.state_transfer import certify_ready_bounded_batch as cli
from app.state_transfer import ready_bounded_batch_certification as certification


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


def _result():
    return {
        "success": True,
        "mode": "READ_ONLY_FIVE_PIN_READY_BATCH_CERTIFICATION",
        "terminal_stage": "COMPLETE",
        "database_transactions": 1,
        "database_writes": 0,
        "object_storage_reads": 0,
        "object_storage_writes": 0,
        "provider_calls": 0,
        "provider_reads": 0,
        "provider_writes": 0,
        "buffer_calls": 0,
        "pinterest_calls": 0,
        "oauth_calls": 0,
        "ai_calls": 0,
        "automatic_retries": 0,
        "scheduler_activations": 0,
        "worker_activations": 0,
        "autonomy_activations": 0,
        "provider_attempts_reserved": 0,
        "publishing_admission": "NOT_GRANTED",
        "ready_batch_certification": "PASS",
        "contract": "FIVE_PIN_READY_BATCH_CERTIFICATION_V1",
        "database_revision": "0034",
        "routine_state": "PAUSED",
        "batch_id": "batch",
        "batch_manifest_sha256": "1" * 64,
        "batch_state": "READY",
        "target_count": 5,
        "attempts_reserved": 0,
        "admission_closed": False,
        "plan_id": "plan",
        "plan_fingerprint": "2" * 64,
        "candidate_count": 5,
        "entries": [
            {
                "slot": i,
                "item_id": f"item-{i}",
                "product_id": f"product-{i}",
                "pinterest_board_record_id": "provider-board",
                "external_board_id": "external-board",
                "item_fingerprint": f"{100 + i:064x}",
                "publication_id": f"pub-{i}",
                "permit_id": f"permit-{i}",
                "publication_fingerprint": f"{200 + i:064x}",
                "request_fingerprint": f"{300 + i:064x}",
                "scheduled_for": "2026-10-07T10:00:00+00:00",
                "permit_expires_at": "2026-10-08T10:00:00+00:00",
                "creative_id": f"creative-{i}",
                "creative_sha256": f"{400 + i:064x}",
                "creative_size_bytes": 1000 + i,
                "creative_rendered_url": f"/api/pins/creatives/creative-{i}/image",
            }
            for i in range(5)
        ],
        "ready_batch_fingerprint": "3" * 64,
    }


def _login(client):
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "secret"},
    )
    assert response.status_code == 200


def test_ready_route_requires_real_admin(client, monkeypatch):
    calls = []
    monkeypatch.setattr(route, "run_ready_batch_certification", lambda: calls.append(1))
    response = client.get(
        "/api/internal/operations/bounded-pilot-ready",
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 401
    assert calls == []


def test_ready_route_requires_allowed_origin(client, monkeypatch):
    _login(client)
    calls = []
    monkeypatch.setattr(route, "run_ready_batch_certification", lambda: calls.append(1))
    response = client.get("/api/internal/operations/bounded-pilot-ready")
    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    assert calls == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"params": {"batch_id": "forbidden"}},
        {"content": b'{"batch_id":"forbidden"}'},
    ],
)
def test_ready_route_rejects_all_inputs_before_certification(client, monkeypatch, kwargs):
    _login(client)
    calls = []
    monkeypatch.setattr(route, "run_ready_batch_certification", lambda: calls.append(1))
    response = client.request(
        "GET",
        "/api/internal/operations/bounded-pilot-ready",
        headers={"Origin": ORIGIN},
        **kwargs,
    )
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["code"] == "REQUEST_INPUTS_PROHIBITED"
    assert calls == []


def test_ready_route_returns_exact_dossier_once(client, monkeypatch):
    _login(client)
    calls = []

    def fake():
        calls.append(1)
        return _result()

    monkeypatch.setattr(route, "run_ready_batch_certification", fake)
    response = client.get(
        "/api/internal/operations/bounded-pilot-ready",
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == _result()
    assert calls == [1]
    assert len(response.json()["entries"]) == 5
    assert response.json()["attempts_reserved"] == 0
    assert response.json()["publishing_admission"] == "NOT_GRANTED"


def test_ready_route_maps_refusal_and_sanitizes_exception(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(route, "run_ready_batch_certification", lambda: {
        "success": False,
        "mode": "READ_ONLY_FIVE_PIN_READY_BATCH_CERTIFICATION",
        "terminal_stage": "READ_ONLY_DATABASE",
        "ready_batch_certification": "NOT_GRANTED",
        "publishing_admission": "NOT_GRANTED",
    })
    refused = client.get(
        "/api/internal/operations/bounded-pilot-ready",
        headers={"Origin": ORIGIN},
    )
    assert refused.status_code == 409
    assert refused.headers["cache-control"] == "no-store"

    def boom():
        raise RuntimeError("PRIVATE_DATABASE_URL_AND_SECRET")

    monkeypatch.setattr(route, "run_ready_batch_certification", boom)
    unexpected = client.get(
        "/api/internal/operations/bounded-pilot-ready",
        headers={"Origin": ORIGIN},
    )
    assert unexpected.status_code == 409
    assert "PRIVATE_DATABASE_URL_AND_SECRET" not in unexpected.text
    assert unexpected.json()["terminal_stage"] == "UNEXPECTED"


def test_ready_cli_arguments_refused_without_echo(capsys):
    secret = "PRIVATE_BATCH_ARGUMENT"
    assert cli.main(["--batch-id", secret]) == 2
    output = capsys.readouterr()
    assert output.err == ""
    assert secret not in output.out
    result = json.loads(output.out)
    assert result["terminal_stage"] == "ARGUMENTS"
    assert result["ready_batch_certification"] == "NOT_GRANTED"
    assert result["publishing_admission"] == "NOT_GRANTED"


def test_ready_certification_source_is_read_only_and_provider_free():
    source = inspect.getsource(certification)
    route_source = inspect.getsource(route)
    forbidden = (
        "BufferGateway",
        "PinterestClient",
        "create_pinterest_post",
        "run_bounded_batch_once",
        "create_batch(",
        "prepare_batch(",
        "execute_autonomous_item",
        "INSERT ",
        "UPDATE ",
        "DELETE ",
        "S3ExactTarget",
        "PNGMediaStorage",
        "CreativeStorage",
    )
    for token in forbidden:
        assert token not in source
    assert "transaction(engine, readonly=True)" in source
    assert "gate_snapshot()" in source
    assert "provider_calls" in source
    assert "@router.get" in route_source
    assert "@router.post" not in route_source
    assert "require_real_admin" in route_source
