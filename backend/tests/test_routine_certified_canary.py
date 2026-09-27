from __future__ import annotations

import os
import socket
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.core import auth
from app.core.config import get_settings
from app.db.base import Base
from app.db.session import get_db
from app.models.domain import (
    ContentAngle, CreativeTemplate, PinApproval, PinConcept, PinCreative, PinDraft,
    PinPublication, PinterestBoard, PinterestConnection, Product, ProductImage,
    PublicationAttempt, PublicationStatus, Store,
)
from app.models.routine_publishing import (
    RoutineAttemptBoundary, RoutineDispatchPermit, RoutinePublishingControl, RoutinePublishingRun,
)
from app.services import routine_certified_canary as certified
from test_routine_canary_fixture import NOW, _db, _scheduler, _settings


def _permit(publication, *, ident="permit-1", expires_at=None):
    return RoutineDispatchPermit(
        id=ident, publication_id=publication.id, dispatch_provider="buffer",
        approval_id=publication.approval_id,
        pinterest_board_record_id=publication.pinterest_board_record_id,
        publication_fingerprint=publication.publication_fingerprint,
        request_fingerprint="d" * 64,
        scheduled_for_snapshot=publication.scheduled_for,
        quality_policy_version="pinterest-quality-v1",
        quality_snapshot={"status": "PASS"}, duplicate_snapshot={"status": "SAFE_TO_CONTINUE"},
        readiness_snapshot={"dispatch_provider": "buffer"}, authorized_by="test",
        authorized_at=NOW - timedelta(minutes=5),
        expires_at=expires_at or NOW + timedelta(hours=1), status="ACTIVE",
    )


def _ready(db):
    publication = db.get(PinPublication, "canary-source")
    publication.status = PublicationStatus.SCHEDULED
    publication.scheduled_for = NOW - timedelta(minutes=1)
    db.add(_permit(publication))
    db.commit()


def _evidence(publication_id="canary-source", external_requests=0):
    return SimpleNamespace(
        publication_id=publication_id, permit_validated=True, quality_passed=True,
        duplicate_safe=True, persisted_route_validated=True, external_requests=external_requests,
    )


@pytest.fixture
def canary(monkeypatch):
    engine, db = _db()
    _ready(db)
    monkeypatch.setattr(
        certified, "validate_permit",
        lambda _db, publication, permit, *, now, require_due: {
            "valid": permit.expires_at.replace(tzinfo=NOW.tzinfo) > now
            and permit.publication_id == publication.id,
        },
    )
    monkeypatch.setattr(
        certified, "build_routine_offline_evidence",
        lambda _db, publication, **kwargs: _evidence(publication.id),
    )
    yield db
    db.close()
    engine.dispose()


def _run(db, **kwargs):
    publication_id = kwargs.pop("publication_id", "canary-source")
    settings = kwargs.pop("settings", _settings())
    if not settings.app_secret_key:
        settings = settings.model_copy(update={"app_secret_key": "s" * 48})
    scheduler = kwargs.pop("scheduler_snapshot", _scheduler())
    preflight_receipt = kwargs.pop("preflight_receipt", None)
    if preflight_receipt is None:
        preflight_receipt = certified.preflight_certified_offline_canary(
            db, publication_id=publication_id, settings=settings, now=NOW,
            scheduler_snapshot=scheduler,
        )
    return certified.certify_one_shot_offline_canary(
        db, publication_id=publication_id, settings=settings,
        preflight_contract_version=certified.PREFLIGHT_CONTRACT_VERSION,
        preflight_receipt=preflight_receipt, now=NOW, scheduler_snapshot=scheduler,
    )


def _unchanged(db):
    assert db.get(RoutinePublishingControl, "default").state == "PAUSED"
    assert db.get(PinPublication, "canary-source").status == PublicationStatus.SCHEDULED
    permit = db.scalar(select(RoutineDispatchPermit).where(
        RoutineDispatchPermit.publication_id == "canary-source",
        RoutineDispatchPermit.status == "ACTIVE",
    ))
    assert permit is not None
    assert permit.status == "ACTIVE" and permit.consumed_at is None
    assert db.scalars(select(PublicationAttempt)).all() == []
    assert db.scalars(select(RoutineAttemptBoundary)).all() == []


def test_exact_offline_receipt_preserves_every_persisted_boundary(canary, monkeypatch):
    from app.services import routine_pinterest_worker, routine_buffer_dispatch

    def forbidden(*args, **kwargs):
        raise AssertionError("worker/dispatch/reconciliation must not run")

    monkeypatch.setattr(routine_pinterest_worker, "run_once", forbidden)
    monkeypatch.setattr(routine_buffer_dispatch, "dispatch_routine_buffer", forbidden)
    monkeypatch.setattr(routine_buffer_dispatch, "recover_stale_routine_claims", forbidden)
    before = len(canary.scalars(select(RoutinePublishingRun)).all())
    assert _run(canary) == {
        "status": "SUCCEEDED", "mode": "DRY_RUN", "publication_id": "canary-source",
        "control_state": "PAUSED", "scanned": 1, "eligible": 1, "skipped": 0,
        "claimed": 0, "dispatched": 0, "published": 0, "failed": 0, "unknown": 0,
        "external_requests": 0,
    }
    _unchanged(canary)
    assert len(canary.scalars(select(RoutinePublishingRun)).all()) == before


@pytest.mark.parametrize(
    ("kind", "code"),
    [
        ("none", "CANARY_DUE_IDENTITY_OR_CARDINALITY"),
        ("extra", "CANARY_DUE_IDENTITY_OR_CARDINALITY"),
        ("wrong", "CANARY_DUE_IDENTITY_OR_CARDINALITY"),
        ("missing_permit", "CANARY_ACTIVE_PERMIT_CARDINALITY"),
        ("expired_permit", "CANARY_PERMIT_INVALID"),
        ("extra_permit", "CANARY_ACTIVE_PERMIT_CARDINALITY"),
        ("stale_routing", "PERSISTED_PINTEREST_ROUTING_STALE"),
        ("bad_evidence", "CANARY_OFFLINE_EVIDENCE_INVALID"),
        ("network", "CANARY_NETWORK_OPERATION_ATTEMPTED"),
        ("existing_socket", "CANARY_NETWORK_OPERATION_ATTEMPTED"),
        ("write", "CANARY_UNEXPECTED_DATABASE_WRITE"),
        ("unexpected", "division by zero"),
        ("non_paused", "ROUTINE_CONTROL_NOT_PAUSED"),
        ("running_audit", "ROUTINE_WORKER_ALREADY_RUNNING"),
        ("scheduler", "ROUTINE_SCHEDULER_NOT_DORMANT"),
        ("gate", "UNSAFE_RUNTIME_GATE_STATE"),
    ],
)
def test_failures_leave_control_and_canary_untouched(canary, monkeypatch, kind, code):
    settings = _settings()
    scheduler = _scheduler()
    publication_id = "canary-source"
    if kind == "none":
        canary.get(PinPublication, publication_id).scheduled_for = NOW + timedelta(minutes=5)
    elif kind in ("extra", "extra_permit"):
        original = canary.get(PinPublication, publication_id)
        fields = {col.name: getattr(original, col.name) for col in PinPublication.__table__.columns}
        fields["id"] = "other-canary"
        fields["publication_fingerprint"] = "f" * 64
        fields["scheduled_for"] = NOW - timedelta(minutes=2) if kind == "extra" else NOW + timedelta(minutes=2)
        other = PinPublication(**fields)
        canary.add(other)
        canary.flush()
        if kind == "extra_permit":
            canary.add(_permit(other, ident="permit-extra"))
    elif kind == "wrong":
        publication_id = "not-the-canary"
    elif kind == "missing_permit":
        canary.delete(canary.get(RoutineDispatchPermit, "permit-1"))
    elif kind == "expired_permit":
        canary.get(RoutineDispatchPermit, "permit-1").expires_at = NOW - timedelta(seconds=1)
    elif kind == "stale_routing":
        from app.services import routine_offline_preflight as offline
        monkeypatch.setattr(
            offline, "validate_permit", lambda *a, **k: {
                "valid": True, "quality": {"status": "PASS"},
                "duplicate": {"status": "SAFE_TO_CONTINUE"},
            },
        )
        monkeypatch.setattr(certified, "build_routine_offline_evidence", offline.build_routine_offline_evidence)
    elif kind == "bad_evidence":
        monkeypatch.setattr(
            certified, "build_routine_offline_evidence",
            lambda *a, **k: _evidence(external_requests=1),
        )
    elif kind == "network":
        def network(*args, **kwargs):
            # The audit hook raises before connecting; no provider or server is contacted.
            with socket.socket() as sock:
                sock.connect(("127.0.0.1", 9))
        monkeypatch.setattr(certified, "build_routine_offline_evidence", network)
    elif kind == "existing_socket":
        left, right = socket.socketpair()
        def existing_socket(*args, **kwargs):
            left.send(b"would-be-external-request")
        monkeypatch.setattr(certified, "build_routine_offline_evidence", existing_socket)
    elif kind == "write":
        def write(db, publication, **kwargs):
            publication.status = PublicationStatus.PUBLISHING
            return _evidence()
        monkeypatch.setattr(certified, "build_routine_offline_evidence", write)
    elif kind == "unexpected":
        monkeypatch.setattr(certified, "build_routine_offline_evidence", lambda *a, **k: 1 / 0)
    elif kind == "non_paused":
        canary.get(RoutinePublishingControl, "default").state = "DRY_RUN"
    elif kind == "running_audit":
        canary.add(RoutinePublishingRun(
            id="unrelated", mode="DRY_RUN", status="RUNNING", started_at=NOW,
            heartbeat_at=NOW - timedelta(days=2),
        ))
    elif kind == "scheduler":
        scheduler["task_running"] = True
    elif kind == "gate":
        settings = _settings(pinterest_autonomous_execution_enabled=True)

    if kind in {"none", "extra", "extra_permit", "missing_permit", "expired_permit",
                "non_paused", "running_audit"}:
        canary.commit()
    try:
        with pytest.raises((certified.CertifiedCanaryError, ZeroDivisionError), match=code):
            _run(canary, publication_id=publication_id, settings=settings, scheduler_snapshot=scheduler)
    finally:
        if kind == "existing_socket":
            left.close()
            right.close()
        assert canary.get(RoutinePublishingControl, "default").state == (
            "DRY_RUN" if kind == "non_paused" else "PAUSED"
        )
        if kind not in {"none", "missing_permit", "expired_permit", "non_paused"}:
            _unchanged(canary)
        if kind == "running_audit":
            row = canary.get(RoutinePublishingRun, "unrelated")
            assert row.status == "RUNNING" and row.error_code is None


def test_api_auth_origin_confirmation_and_sanitized_receipt(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("APP_SECRET_KEY", "s" * 48)
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", auth.hash_password("test-only-password"))
    monkeypatch.setenv("AUTH_ALLOWED_ORIGINS", "http://localhost:5000")
    monkeypatch.delenv("REPLIT_DEPLOYMENT", raising=False)
    get_settings.cache_clear()
    from app.main import app
    calls = []
    receipt = {"contract_version": certified.PREFLIGHT_CONTRACT_VERSION}
    monkeypatch.setattr(
        certified, "preflight_certified_offline_canary",
        lambda db, *, publication_id, settings: calls.append(("preflight", publication_id)) or receipt,
    )
    monkeypatch.setattr(
        certified, "certify_one_shot_offline_canary",
        lambda db, *, publication_id, settings, preflight_contract_version, preflight_receipt:
        calls.append(("execute", publication_id, preflight_contract_version, preflight_receipt)) or
        {"status": "SUCCEEDED", "external_requests": 0},
    )
    app.dependency_overrides[get_db] = lambda: object()
    client = TestClient(app)
    path = "/api/routine-publishing/publications/canary-source/certified-offline-dry-run"
    preflight_path = "/api/routine-publishing/publications/canary-source/certified-offline-preflight"
    body = {
        "confirmed": True, "confirmation_text_version": certified.CONFIRMATION_TEXT_VERSION,
        "preflight_contract_version": certified.PREFLIGHT_CONTRACT_VERSION,
        "preflight_receipt": receipt,
    }
    try:
        assert client.get(preflight_path).status_code == 401
        assert client.post(path, json=body, headers={"Origin": "http://localhost:5000"}).status_code == 401
        assert client.post(path, json=body).status_code == 403
        assert client.post(path, json=body, headers={"Origin": "https://other.example"}).status_code == 403
        assert client.get(preflight_path, headers={"Origin": "https://other.example"}).status_code == 403
        login = client.post("/api/auth/login", json={
            "username": "admin", "password": "test-only-password",
        }, headers={"Origin": "http://localhost:5000"})
        assert login.status_code == 200
        # TestClient runs on HTTP; use a signed test session even if the
        # login response set a secure cookie for the proxied environment.
        client.cookies.set(auth.SESSION_COOKIE, auth.make_session("admin"))
        headers = {"Origin": "http://localhost:5000"}
        assert client.get(preflight_path, headers=headers).json() == receipt
        for bad in ({"confirmed": False, "confirmation_text_version": body["confirmation_text_version"]},
                    {"confirmed": True, "confirmation_text_version": "WRONG"},
                    {**body, "unexpected": True}):
            assert client.post(path, json=bad, headers=headers).status_code == 422
        assert client.post(path, json={
            "confirmed": True, "confirmation_text_version": certified.CONFIRMATION_TEXT_VERSION,
        }, headers=headers).status_code == 422
        response = client.post(path, json=body, headers=headers)
        assert response.status_code == 200
        assert response.json() == {"status": "SUCCEEDED", "external_requests": 0}
        assert calls == [
            ("preflight", "canary-source"),
            ("execute", "canary-source", certified.PREFLIGHT_CONTRACT_VERSION, receipt),
        ]
    finally:
        app.dependency_overrides.pop(get_db, None)
        get_settings.cache_clear()


@pytest.mark.skipif(not os.getenv("TASK595_POSTGRES_URL"), reason="isolated Task 59.5 PostgreSQL URL not set")
@pytest.mark.parametrize("case", [
    "success", "real_success", "zero", "multiple", "wrong", "expired", "extra_permit",
    "stale_routing", "network", "unrelated_running", "concurrent_writer",
    "state_drift", "preflight_write", "execution_select_into",
])
def test_postgres_locked_certification_preserves_paused_and_unrelated_run(monkeypatch, case):
    # CI supplies a disposable database, never a production URL.
    url = make_url(os.environ["TASK595_POSTGRES_URL"])
    assert url.host in {"127.0.0.1", "localhost"} and url.database.startswith("sm_poster_task595")
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    db = Session()
    try:
        db.add(RoutinePublishingControl(id="default", state="PAUSED", paused_at=NOW, paused_by="test"))
        db.add(Store(id="store", name="Canary", shop_domain="canary.example"))
        db.add(ContentAngle(id="angle", key="canary", name="Canary"))
        db.flush()
        db.add(Product(id="product", store_id="store", shopify_product_id="product",
                       handle="canary", title="Canary", product_url="https://canary.example/p/canary",
                       price_min=Decimal("19.99"), compare_at_min=Decimal("24.00")))
        db.flush()
        db.add(PinConcept(id="concept", store_id="store", product_id="product",
                          content_angle_id="angle", fingerprint="e" * 64))
        db.flush()
        db.add(PinDraft(
            id="draft-1", concept_id="concept", version=1, title="Canary",
            description="Canary", alt_text="Canary",
            destination_url="https://canary.example/p/canary",
            utm_url="https://canary.example/p/canary?utm_source=pinterest",
            text_fingerprint="b" * 64, status="APPROVED",
        ))
        db.add(ProductImage(id="source-1", product_id="product",
                            source_url="https://cdn.example/canary.jpg", is_primary=True))
        db.add(CreativeTemplate(id="template-1", key="template", version=1, name="Canary"))
        db.flush()
        db.add(PinCreative(
            id="creative-1", draft_id="draft-1", template_id="template-1",
            source_image_id="source-1", creative_fingerprint="c" * 64, render_status="RENDERED",
            rendered_url="https://images.diamondshelf.us/canary.jpg", width=1000, height=1500,
        ))
        db.flush()
        db.add(PinApproval(id="approval-1", draft_id="draft-1", creative_id="creative-1",
                           decision="APPROVED", decided_by="test"))
        db.add(PinterestConnection(
            id="conn-1", external_user_id="test-user", access_token_ciphertext="test-only",
            refresh_token_ciphertext="test-only", status="CONNECTED", boards_last_synced_at=NOW,
        ))
        db.flush()
        db.add(PinterestBoard(
            id="board-1", connection_id="conn-1", external_board_id="external-board",
            name="Canary", is_active=True, is_eligible=True,
            routing_label="canary", last_synced_at=NOW,
        ))
        db.flush()
        db.add(PinPublication(
            id="canary-source", draft_id="draft-1", creative_id="creative-1",
            approval_id="approval-1", pinterest_connection_id="conn-1",
            pinterest_board_record_id="board-1", pinterest_board_id_snapshot="external-board",
            publication_fingerprint="a" * 64, text_fingerprint="b" * 64,
            creative_fingerprint="c" * 64, status=PublicationStatus.SCHEDULED,
            scheduled_for=NOW - timedelta(minutes=1), title_snapshot="Canary",
            description_snapshot="A canary product presented with clear details for visual discovery.",
            alt_text_snapshot="Canary product shown in a simple studio setting.",
            destination_url="https://diamondshelf.us/products/canary",
            utm_url="https://diamondshelf.us/products/canary?utm_source=pinterest",
            media_url_snapshot="https://images.diamondshelf.us/canary.jpg",
            source_image_id="source-1", template_id="template-1",
            template_key="template", template_version=1,
        ))
        db.flush()
        if case == "real_success":
            from app.services.pinterest_publication_quality import validate_publication_quality
            from app.services.routine_dispatch_authorization import create_permit

            publication = db.get(PinPublication, "canary-source")
            quality = validate_publication_quality(db, publication, dispatch_provider="buffer")
            assert quality["status"] == "PASS", [
                item["code"] for item in quality["checks"] if not item["passed"]
            ]
            create_permit(db, publication, actor="test", now=NOW, commit=False)
        else:
            db.add(_permit(db.get(PinPublication, "canary-source")))
        db.commit()
        db.add(RoutinePublishingRun(
            id="unrelated", mode="DRY_RUN", status="FAILED", started_at=NOW,
            completed_at=NOW, error_code="HISTORICAL",
        ))
        db.commit()
        if case != "real_success":
            monkeypatch.setattr(
                certified, "validate_permit",
                lambda db_, publication, permit, **k: {"valid": permit.expires_at > NOW},
            )
            monkeypatch.setattr(
                certified, "build_routine_offline_evidence", lambda *a, **k: _evidence(),
            )
        publication_id = "canary-source"
        if case == "zero":
            db.get(PinPublication, publication_id).scheduled_for = NOW + timedelta(minutes=5)
        elif case in {"multiple", "extra_permit"}:
            original = db.get(PinPublication, publication_id)
            fields = {col.name: getattr(original, col.name) for col in PinPublication.__table__.columns}
            fields["id"] = "other-canary"
            fields["publication_fingerprint"] = "f" * 64
            fields["scheduled_for"] = (
                NOW - timedelta(minutes=2) if case == "multiple" else NOW + timedelta(minutes=5)
            )
            db.add(PinPublication(**fields))
            db.flush()
            if case == "extra_permit":
                db.add(_permit(db.get(PinPublication, "other-canary"), ident="permit-extra"))
        elif case == "wrong":
            publication_id = "wrong-canary"
        elif case == "expired":
            db.get(RoutineDispatchPermit, "permit-1").expires_at = NOW - timedelta(seconds=1)
        elif case == "stale_routing":
            db.get(PinterestBoard, "board-1").is_active = False
            from app.services import routine_offline_preflight as offline
            monkeypatch.setattr(
                offline, "validate_permit", lambda *a, **k: {
                    "valid": True, "quality": {"status": "PASS"},
                    "duplicate": {"status": "SAFE_TO_CONTINUE"},
                },
            )
            monkeypatch.setattr(certified, "build_routine_offline_evidence", offline.build_routine_offline_evidence)
        elif case == "network":
            def network(*args, **kwargs):
                with socket.socket() as sock:
                    sock.connect(("127.0.0.1", 9))
            monkeypatch.setattr(certified, "build_routine_offline_evidence", network)
        elif case == "concurrent_writer":
            def try_write(*args, **kwargs):
                with engine.connect() as conn:
                    conn.execute(text("SET LOCAL lock_timeout = '150ms'"))
                    with pytest.raises(OperationalError, match="lock timeout"):
                        conn.execute(text(
                            "UPDATE pin_publications SET scheduled_for = "
                            "scheduled_for + interval '1 minute' WHERE id = 'canary-source'"
                        ))
                    conn.rollback()
                return _evidence()
            monkeypatch.setattr(certified, "build_routine_offline_evidence", try_write)
        elif case == "unrelated_running":
            db.get(RoutinePublishingRun, "unrelated").status = "RUNNING"
            db.get(RoutinePublishingRun, "unrelated").completed_at = None
        if case in {"zero", "multiple", "extra_permit", "expired", "stale_routing", "unrelated_running"}:
            db.commit()
        if case in {"success", "real_success"}:
            assert _run(db)["external_requests"] == 0
        elif case == "concurrent_writer":
            # Preflight does not take the execution lock. Exercise contention
            # after obtaining the read-only receipt, during locked re-evaluation.
            monkeypatch.setattr(certified, "build_routine_offline_evidence", lambda *a, **k: _evidence())
            settings = _settings(app_secret_key="s" * 48)
            preflight_receipt = certified.preflight_certified_offline_canary(
                db, publication_id=publication_id, settings=settings, now=NOW,
                scheduler_snapshot=_scheduler(),
            )
            monkeypatch.setattr(certified, "build_routine_offline_evidence", try_write)
            assert _run(db, settings=settings, preflight_receipt=preflight_receipt)["external_requests"] == 0
        elif case == "state_drift":
            settings = _settings(app_secret_key="s" * 48)
            receipt = certified.preflight_certified_offline_canary(
                db, publication_id=publication_id, settings=settings, now=NOW,
                scheduler_snapshot=_scheduler(),
            )
            db.get(PinPublication, publication_id).title_snapshot = "Drifted after preflight"
            db.commit()
            with pytest.raises(certified.CertifiedCanaryError, match="CANARY_PREFLIGHT_STATE_DRIFT"):
                _run(db, settings=settings, preflight_receipt=receipt)
        elif case == "preflight_write":
            def forbidden_write(db_, *args, **kwargs):
                db_.execute(text(
                    "UPDATE pin_publications SET scheduled_for = scheduled_for "
                    "WHERE id = 'canary-source'"
                ))
                return _evidence()
            monkeypatch.setattr(certified, "build_routine_offline_evidence", forbidden_write)
            with pytest.raises(certified.CertifiedCanaryError, match="CANARY_DATABASE_WRITE_ATTEMPTED"):
                _run(db)
        elif case == "execution_select_into":
            settings = _settings(app_secret_key="s" * 48)
            receipt = certified.preflight_certified_offline_canary(
                db, publication_id=publication_id, settings=settings, now=NOW,
                scheduler_snapshot=_scheduler(),
            )
            def forbidden_select(db_, *args, **kwargs):
                db_.execute(text("SELECT 1 INTO TEMP TABLE canary_probe"))
                return _evidence()
            monkeypatch.setattr(certified, "build_routine_offline_evidence", forbidden_select)
            with pytest.raises(certified.CertifiedCanaryError, match="CANARY_DATABASE_WRITE_ATTEMPTED"):
                _run(db, settings=settings, preflight_receipt=receipt)
            assert db.scalar(text("SELECT to_regclass('pg_temp.canary_probe')")) is None
        else:
            with pytest.raises(certified.CertifiedCanaryError):
                _run(db, publication_id=publication_id)
        _unchanged(db)
        assert db.get(RoutinePublishingRun, "unrelated").error_code == "HISTORICAL"
        assert len(db.scalars(select(RoutinePublishingRun)).all()) == 1
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()