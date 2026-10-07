"""Task 61.57: durable READY-batch receipt regressions."""
from __future__ import annotations

import json
import logging

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes import bounded_pilot_ready_receipt as route
from app.core import auth
from app.core.config import get_settings
from app.db.base import Base
from app.db.session import get_db
from app.models.domain import AuditLog
from app.services import ready_bounded_batch_receipt as receipt
from app.state_transfer import persist_ready_bounded_batch_receipt as cli


ORIGIN = "http://localhost:5000"


def _certification():
    entries = []
    for i in range(5):
        entries.append({
            "slot": i,
            "item_id": f"item-{i}",
            "product_id": f"product-{i}",
            "pinterest_board_record_id": f"board-{i}",
            "external_board_id": f"external-{i}",
            "item_fingerprint": f"{100+i:064x}",
            "publication_id": f"pub-{i}",
            "permit_id": f"permit-{i}",
            "publication_fingerprint": f"{200+i:064x}",
            "request_fingerprint": f"{300+i:064x}",
            "scheduled_for": f"2026-10-{20+i:02d}T12:00:00+00:00",
            "permit_expires_at": f"2026-10-{21+i:02d}T12:00:00+00:00",
            "creative_id": f"creative-{i}",
            "creative_sha256": f"{400+i:064x}",
            "creative_size_bytes": 1000 + i,
            "creative_rendered_url": f"/api/pins/creatives/creative-{i}/image",
        })
    result = {
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
        "batch_id": "batch-ready",
        "batch_manifest_sha256": "1" * 64,
        "ready_batch_fingerprint": "2" * 64,
        "batch_state": "READY",
        "target_count": 5,
        "attempts_reserved": 0,
        "admission_closed": False,
        "plan_id": "plan-ready",
        "plan_fingerprint": "3" * 64,
        "candidate_count": 5,
        "entries": entries,
    }
    return result


@pytest.fixture
def receipt_db():
    engine = sa.create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    AuditLog.__table__.create(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield engine, sessions
    finally:
        engine.dispose()


def test_receipt_persistence_is_deterministic_and_idempotent(receipt_db):
    _, sessions = receipt_db
    with sessions() as db:
        first = receipt.persist_receipt(db, _certification())
        assert first["created"] is True
        assert first["receipt_id"] == receipt.receipt_id("2" * 64)
        assert first["receipt"]["receipt_sha256"]
        assert db.scalar(sa.select(sa.func.count()).select_from(AuditLog)) == 1

    with sessions() as db:
        second = receipt.persist_receipt(db, _certification())
        assert second["created"] is False
        assert second["receipt_id"] == first["receipt_id"]
        assert second["receipt"] == first["receipt"]
        assert db.scalar(sa.select(sa.func.count()).select_from(AuditLog)) == 1


def test_conflicting_or_tampered_receipt_fails_closed(receipt_db):
    _, sessions = receipt_db
    with sessions() as db:
        stored = receipt.persist_receipt(db, _certification())
        row = db.get(AuditLog, stored["receipt_id"])
        changed = dict(row.metadata_json)
        changed["plan_id"] = "tampered-plan"
        row.metadata_json = changed
        db.commit()

    with sessions() as db:
        with pytest.raises(receipt.ReadyReceiptError, match="READY_RECEIPT_CONFLICT"):
            receipt.persist_receipt(db, _certification())
        with pytest.raises(receipt.ReadyReceiptError, match="STORED_READY_RECEIPT_INVALID"):
            receipt.latest_receipt(db)
        assert db.scalar(sa.select(sa.func.count()).select_from(AuditLog)) == 1


def test_runtime_log_emits_only_validated_sanitized_receipt(receipt_db, caplog):
    _, sessions = receipt_db
    with sessions() as db:
        stored = receipt.persist_receipt(db, _certification())

    logger = logging.getLogger("test.ready-receipt")
    with caplog.at_level(logging.INFO, logger=logger.name):
        assert receipt.emit_latest_receipt_to_runtime_log(
            session_factory=sessions,
            logger=logger,
        ) is True

    messages = [record.getMessage() for record in caplog.records]
    line = next(message for message in messages if message.startswith(receipt.LOG_PREFIX))
    parsed = json.loads(line[len(receipt.LOG_PREFIX):])
    assert parsed["receipt_id"] == stored["receipt_id"]
    assert parsed["receipt"]["batch_id"] == "batch-ready"
    assert parsed["receipt"]["candidate_count"] == 5
    assert parsed["receipt"]["provider_calls"] == 0
    assert parsed["receipt"]["publishing_admission"] == "NOT_GRANTED"


def test_writer_cli_certifies_before_single_receipt_write(receipt_db):
    _, sessions = receipt_db
    result = cli.run(
        certification_runner=_certification,
        session_factory=sessions,
    )
    assert result == {
        "success": True,
        "mode": "DURABLE_FIVE_PIN_READY_BATCH_RECEIPT",
        "terminal_stage": "COMPLETE",
        "code": "PASS",
        "database_writes": 1,
        "provider_calls": 0,
        "buffer_calls": 0,
        "pinterest_calls": 0,
        "oauth_calls": 0,
        "ai_calls": 0,
        "publishing_admission": "NOT_GRANTED",
        "receipt_id": receipt.receipt_id("2" * 64),
        "ready_batch_fingerprint": "2" * 64,
        "receipt_sha256": receipt.receipt_from_certification(_certification())["receipt_sha256"],
        "created": True,
    }

    second = cli.run(
        certification_runner=_certification,
        session_factory=sessions,
    )
    assert second["success"] is True
    assert second["created"] is False
    assert second["database_writes"] == 0


def test_writer_cli_refuses_failed_certification_without_write(receipt_db):
    _, sessions = receipt_db
    failed = _certification()
    failed["success"] = False
    failed["ready_batch_certification"] = "NOT_GRANTED"

    result = cli.run(
        certification_runner=lambda: failed,
        session_factory=sessions,
    )
    assert result["success"] is False
    assert result["database_writes"] == 0
    with sessions() as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(AuditLog)) == 0


def test_writer_cli_arguments_are_prohibited(capsys):
    secret = "PRIVATE_SENTINEL"
    assert cli.main(["--receipt", secret]) == 2
    output = capsys.readouterr()
    assert secret not in output.out
    parsed = json.loads(output.out)
    assert parsed["terminal_stage"] == "ARGUMENTS"
    assert parsed["code"] == "ARGUMENTS_PROHIBITED"
    assert parsed["publishing_admission"] == "NOT_GRANTED"


@pytest.fixture
def client(receipt_db, monkeypatch):
    _, sessions = receipt_db
    with sessions() as db:
        receipt.persist_receipt(db, _certification())

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

    def override_db():
        db = sessions()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def _login(client):
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "secret"},
    )
    assert response.status_code == 200


def test_receipt_route_requires_real_admin(client):
    response = client.get(
        "/api/internal/operations/bounded-pilot-ready-receipt",
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 401


def test_receipt_route_returns_validated_receipt_no_store(client):
    _login(client)
    response = client.get(
        "/api/internal/operations/bounded-pilot-ready-receipt",
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["success"] is True
    assert body["receipt"]["batch_id"] == "batch-ready"
    assert body["receipt"]["provider_calls"] == 0
    assert body["publishing_admission"] == "NOT_GRANTED"


def test_receipt_route_rejects_inputs_before_read(client):
    _login(client)
    response = client.get(
        "/api/internal/operations/bounded-pilot-ready-receipt?batch_id=forbidden",
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 409
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["code"] == "REQUEST_INPUTS_PROHIBITED"


def test_receipt_source_has_no_provider_or_live_execution_paths():
    import inspect

    source = inspect.getsource(receipt)
    cli_source = inspect.getsource(cli)
    forbidden = (
        "BufferGateway",
        "PinterestClient",
        "create_pinterest_post",
        "run_bounded_batch_once",
        "reserve_attempt",
        "consume",
        "set_control",
        "UPDATE ",
        "DELETE ",
    )
    for token in forbidden:
        assert token not in source
        assert token not in cli_source
    assert "AuditLog(" in source
    assert "db.add(row)" in source
    assert "db.commit()" in source



def test_railway_production_receipt_logging_gate_uses_railway_identity(monkeypatch):
    monkeypatch.delenv("RAILWAY_ENVIRONMENT_ID", raising=False)
    monkeypatch.delenv("RAILWAY_ENVIRONMENT_NAME", raising=False)
    monkeypatch.setenv("APP_ENV", "development")
    assert receipt.railway_production_receipt_logging_enabled() is False

    monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", "env-id")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "preview")
    assert receipt.railway_production_receipt_logging_enabled() is False

    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")
    assert receipt.railway_production_receipt_logging_enabled() is True


def test_main_receipt_emission_no_longer_depends_on_app_env_label():
    import inspect
    import app.main as main

    source = inspect.getsource(main.lifespan)
    assert "railway_production_receipt_logging_enabled()" in source
    assert "settings.app_env" not in source
