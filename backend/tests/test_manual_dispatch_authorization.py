from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models.domain import (
    CreativeTemplate,
    PinApproval,
    PinCreative,
    PinPublication,
    PinterestBoard,
    PinterestConnection,
    ProductImage,
    PublicationAttempt,
    PublicationDispatchAuthorization,
    PublicationReconciliationEvent,
    PublicationStatus,
)
from app.services.publication_dispatch_authorization import (
    AUTHORIZATION_TTL,
    DispatchAuthorizationError,
    create_authorization,
    _is_active_authorization_unique_violation,
    provider_readiness,
    readiness_result,
    validate_authorization,
)


def _db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _publication(db):
    row = PinPublication(
        id="phase1-publication",
        draft_id="draft",
        creative_id="creative",
        publication_fingerprint="f" * 64,
        status=PublicationStatus.SCHEDULED,
    )
    db.add(row)
    db.commit()
    return row


def _authorization(publication, *, status="ACTIVE", suffix="1"):
    now = datetime.now(timezone.utc)
    return PublicationDispatchAuthorization(
        id=f"auth-{suffix}",
        publication_id=publication.id,
        authorized_by="admin@example.test",
        authorized_at=now,
        publication_fingerprint=publication.publication_fingerprint,
        quality_policy_version="PINTEREST_QUALITY_V1",
        quality_snapshot={"status": "PASS", "checks": []},
        readiness_snapshot={"status": "READY_FOR_MANUAL_DISPATCH"},
        duplicate_snapshot={"status": "SAFE_TO_CONTINUE"},
        confirmation_text_version="CONFIRM_DISPATCH_V1",
        expires_at=now + timedelta(minutes=15),
        status=status,
    )


def _ready_publication(db, *, scheduled_for=None, connection_scopes=None, status=PublicationStatus.SCHEDULED):
    now = datetime.now(timezone.utc)
    template = CreativeTemplate(id="template-ready", key="product_classification", version=1, name="Template")
    source = ProductImage(id="source-ready", product_id="product-ready", source_url="https://cdn.shopify.com/source.jpg", width=1000, height=1500)
    creative = PinCreative(
        id="creative-ready",
        draft_id="draft-ready",
        template_id=template.id,
        source_image_id=source.id,
        rendered_url="https://cdn.shopify.com/creative.jpg",
        creative_fingerprint="c" * 64,
        width=1000,
        height=1500,
        render_status="COMPLETE",
    )
    approval = PinApproval(
        id="approval-ready",
        draft_id="draft-ready",
        revision_id="revision-ready",
        creative_id=creative.id,
        decision="APPROVED",
        decided_by="admin@example.test",
    )
    connection = PinterestConnection(
        id="connection-ready",
        external_user_id="user-ready",
        access_token_ciphertext="enc-access",
        refresh_token_ciphertext="enc-refresh",
        granted_scopes=connection_scopes or ["user_accounts:read", "boards:read", "pins:read"],
        status="CONNECTED",
    )
    board = PinterestBoard(
        id="board-ready",
        connection_id=connection.id,
        external_board_id="external-board-ready",
        name="Fragrance",
        is_active=True,
        is_eligible=True,
        routing_label="fragrance",
    )
    publication = PinPublication(
        id="publication-ready",
        draft_id="draft-ready",
        revision_id="revision-ready",
        creative_id=creative.id,
        approval_id=approval.id,
        source_image_id=source.id,
        template_id=template.id,
        template_key=template.key,
        template_version=template.version,
        text_fingerprint="t" * 64,
        creative_fingerprint=creative.creative_fingerprint,
        pinterest_connection_id=connection.id,
        pinterest_board_record_id=board.id,
        pinterest_board_id_snapshot=board.external_board_id,
        title_snapshot="Fragrance gift pick",
        description_snapshot="Explore this fragrance gift pick for a polished scent routine.",
        alt_text_snapshot="A verified product creative for a fragrance gift pick.",
        destination_url="https://diamondshelf.us/products/fragrance-pick",
        utm_url="https://diamondshelf.us/products/fragrance-pick?utm_source=pinterest",
        media_url_snapshot=creative.rendered_url,
        publication_fingerprint="p" * 64,
        status=status,
        scheduled_for=scheduled_for or (now - timedelta(minutes=1)),
    )
    db.add_all([template, source, creative, approval, connection, board, publication])
    db.commit()
    return publication


def test_dispatch_authorization_stores_exact_fingerprint_policy_snapshots_and_bounded_expiry():
    db = _db()
    publication = _publication(db)
    auth = _authorization(publication)
    db.add(auth)
    db.commit()

    persisted = db.get(PublicationDispatchAuthorization, auth.id)
    assert persisted.authorized_by == "admin@example.test"
    assert persisted.publication_fingerprint == publication.publication_fingerprint
    assert persisted.quality_policy_version == "PINTEREST_QUALITY_V1"
    assert persisted.quality_snapshot == {"status": "PASS", "checks": []}
    assert persisted.readiness_snapshot == {"status": "READY_FOR_MANUAL_DISPATCH"}
    assert persisted.duplicate_snapshot == {"status": "SAFE_TO_CONTINUE"}
    assert persisted.confirmation_text_version == "CONFIRM_DISPATCH_V1"
    assert persisted.status == "ACTIVE"
    assert persisted.expires_at - persisted.authorized_at == timedelta(minutes=15)


def test_dispatch_authorization_requires_server_derived_actor_and_single_active_row():
    db = _db()
    publication = _publication(db)
    db.add(_authorization(publication, suffix="1"))
    db.commit()

    db.add(_authorization(publication, suffix="2"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    first = db.get(PublicationDispatchAuthorization, "auth-1")
    first.status = "CONSUMED"
    first.consumed_at = datetime.now(timezone.utc)
    db.commit()

    db.add(_authorization(publication, suffix="3"))
    db.commit()
    assert db.get(PublicationDispatchAuthorization, "auth-1").status == "CONSUMED"
    assert db.get(PublicationDispatchAuthorization, "auth-3").status == "ACTIVE"

    missing_actor = _authorization(publication, status="REVOKED", suffix="4")
    missing_actor.authorized_by = None
    db.add(missing_actor)
    with pytest.raises(IntegrityError):
        db.commit()


@pytest.mark.parametrize("status", ["CONSUMED", "REVOKED", "EXPIRED"])
def test_non_active_authorizations_remain_auditable(status):
    db = _db()
    publication = _publication(db)
    auth = _authorization(publication, status=status, suffix=status.lower())
    if status == "CONSUMED":
        auth.consumed_at = datetime.now(timezone.utc)
    if status == "REVOKED":
        auth.revoked_at = datetime.now(timezone.utc)
        auth.revoke_reason = "operator revoked"
    db.add(auth)
    db.commit()
    assert db.get(PublicationDispatchAuthorization, auth.id).status == status


def test_dispatch_authorization_rejects_unknown_status():
    db = _db()
    publication = _publication(db)
    db.add(_authorization(publication, status="UNKNOWN_STATUS", suffix="bad"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_reconciliation_event_stores_safe_operator_audit_without_credential_fields():
    db = _db()
    publication = _publication(db)
    attempt = PublicationAttempt(
        id="attempt-1",
        publication_id=publication.id,
        attempt_number=1,
        status="UNKNOWN",
        safe_response_metadata={},
    )
    db.add(attempt)
    db.commit()

    event = PublicationReconciliationEvent(
        id="reconciliation-1",
        publication_id=publication.id,
        attempt_id=attempt.id,
        actor="admin@example.test",
        action="PROVIDER_PIN_CONFIRMED",
        previous_status="PUBLISH_UNKNOWN",
        new_status="PUBLISHED",
        provider_pin_id="pin-123",
        reason="operator confirmed provider Pin",
    )
    db.add(event)
    db.commit()

    persisted = db.get(PublicationReconciliationEvent, event.id)
    assert persisted.actor == "admin@example.test"
    assert persisted.action == "PROVIDER_PIN_CONFIRMED"
    assert persisted.previous_status == "PUBLISH_UNKNOWN"
    assert persisted.new_status == "PUBLISHED"
    assert persisted.provider_pin_id == "pin-123"
    assert not hasattr(persisted, "access_token")
    assert not hasattr(persisted, "refresh_token")
    assert not hasattr(persisted, "raw_body")
    assert not hasattr(persisted, "traceback")


def test_reconciliation_event_allows_only_authorized_task39_transitions():
    db = _db()
    publication = _publication(db)
    attempt = PublicationAttempt(
        id="attempt-1",
        publication_id=publication.id,
        attempt_number=1,
        status="UNKNOWN",
        safe_response_metadata={},
    )
    db.add(attempt)
    db.commit()

    db.add(
        PublicationReconciliationEvent(
            id="valid-published",
            publication_id=publication.id,
            attempt_id=attempt.id,
            actor="admin@example.test",
            action="PROVIDER_PIN_CONFIRMED",
            previous_status="PUBLISH_UNKNOWN",
            new_status="PUBLISHED",
            provider_pin_id="pin-123",
        )
    )
    db.add(
        PublicationReconciliationEvent(
            id="valid-cancelled",
            publication_id=publication.id,
            attempt_id=attempt.id,
            actor="admin@example.test",
            action="CANCELLED_UNKNOWN",
            previous_status="PUBLISH_UNKNOWN",
            new_status="CANCELLED",
            reason="operator cancelled unknown outcome",
        )
    )
    db.commit()
    assert db.get(PublicationReconciliationEvent, "valid-published").created_at is not None
    assert db.get(PublicationReconciliationEvent, "valid-cancelled").created_at is not None


@pytest.mark.parametrize(
    ("action", "previous_status", "new_status"),
    [
        ("RETRY_UNKNOWN", "PUBLISH_UNKNOWN", "PUBLISHED"),
        ("PROVIDER_PIN_CONFIRMED", "PUBLISH_FAILED", "PUBLISHED"),
        ("PROVIDER_PIN_CONFIRMED", "PUBLISH_UNKNOWN", "CANCELLED"),
        ("CANCELLED_UNKNOWN", "PUBLISH_UNKNOWN", "PUBLISHED"),
    ],
)
def test_reconciliation_event_rejects_invalid_actions_and_transitions(action, previous_status, new_status):
    db = _db()
    publication = _publication(db)
    event = PublicationReconciliationEvent(
        id="invalid-reconciliation",
        publication_id=publication.id,
        actor="admin@example.test",
        action=action,
        previous_status=previous_status,
        new_status=new_status,
    )
    db.add(event)
    with pytest.raises(IntegrityError):
        db.commit()


def test_authorization_creation_requires_quality_pass_and_due_scheduled_publication(monkeypatch):
    db = _db()
    publication = _ready_publication(db)
    auth = create_authorization(db, publication, actor="admin@example.test")
    assert auth.expires_at - auth.authorized_at == AUTHORIZATION_TTL
    assert auth.publication_fingerprint == publication.publication_fingerprint
    assert auth.quality_snapshot["status"] == "PASS"
    assert auth.duplicate_snapshot["status"] == "SAFE_TO_CONTINUE"
    assert auth.readiness_snapshot["status"] == "READY_FOR_MANUAL_DISPATCH"

    db2 = _db()
    warning_publication = _ready_publication(db2, scheduled_for=datetime.now(timezone.utc) - timedelta(minutes=1))
    warning_publication.media_url_snapshot = "https://cdn.shopify.com/creative-square.jpg"
    db2.get(PinCreative, warning_publication.creative_id).rendered_url = warning_publication.media_url_snapshot
    db2.get(PinCreative, warning_publication.creative_id).width = 1200
    db2.get(PinCreative, warning_publication.creative_id).height = 1200
    db2.commit()
    with pytest.raises(DispatchAuthorizationError, match="QUALITY_WARNING"):
        create_authorization(db2, warning_publication, actor="admin@example.test")

    db3 = _db()
    fail_publication = _ready_publication(db3)
    fail_publication.title_snapshot = ""
    db3.commit()
    with pytest.raises(DispatchAuthorizationError, match="QUALITY_FAILED"):
        create_authorization(db3, fail_publication, actor="admin@example.test")

    db4 = _db()
    future_publication = _ready_publication(db4, scheduled_for=datetime.now(timezone.utc) + timedelta(hours=1))
    with pytest.raises(DispatchAuthorizationError, match="NOT_DUE"):
        create_authorization(db4, future_publication, actor="admin@example.test")


def test_authorization_creation_expires_stale_active_before_new_active():
    db = _db()
    publication = _ready_publication(db)
    stale = _authorization(publication, suffix="stale")
    stale.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.add(stale)
    db.commit()
    created = create_authorization(db, publication, actor="admin@example.test")
    db.refresh(stale)
    assert stale.status == "EXPIRED"
    assert created.status == "ACTIVE"


def test_authorization_validation_rejects_terminal_and_materially_changed_snapshots():
    db = _db()
    publication = _ready_publication(db)
    auth = create_authorization(db, publication, actor="admin@example.test")

    publication.title_snapshot = "Different title"
    db.commit()
    assert validate_authorization(db, publication, auth)["status"] == "AUTHORIZATION_MISMATCH"

    publication.title_snapshot = "Fragrance gift pick"
    auth.status = "REVOKED"
    db.commit()
    assert validate_authorization(db, publication, auth)["status"] == "AUTHORIZATION_REVOKED"
    auth.status = "EXPIRED"
    db.commit()
    assert validate_authorization(db, publication, auth)["status"] == "AUTHORIZATION_EXPIRED"
    auth.status = "CONSUMED"
    db.commit()
    assert validate_authorization(db, publication, auth)["status"] == "AUTHORIZATION_CONSUMED"


def test_authorization_creation_ignores_provider_kill_switches(monkeypatch):
    from app.services import publication_dispatch_authorization as auth_service

    db = _db()
    publication = _ready_publication(db, connection_scopes=["user_accounts:read", "boards:read", "pins:read"])
    monkeypatch.setattr(auth_service, "get_settings", lambda: type("S", (), {"publishing_enabled": False})())
    auth = create_authorization(db, publication, actor="admin@example.test")
    assert auth.status == "ACTIVE"
    assert "pins:write" not in db.get(PinterestConnection, publication.pinterest_connection_id).granted_scopes
    readiness = readiness_result(db, publication)
    assert readiness["manual_status"] == "READY_FOR_MANUAL_DISPATCH"
    assert readiness["provider_status"] == "PUBLISHING_DISABLED"


def test_authorization_creation_requires_text_fingerprint():
    db = _db()
    publication = _ready_publication(db)
    publication.text_fingerprint = None
    db.commit()

    with pytest.raises(DispatchAuthorizationError, match="INCOMPLETE_SNAPSHOT"):
        create_authorization(db, publication, actor="admin@example.test")

    publication.text_fingerprint = ""
    db.commit()
    with pytest.raises(DispatchAuthorizationError, match="INCOMPLETE_SNAPSHOT"):
        create_authorization(db, publication, actor="admin@example.test")

    readiness = readiness_result(db, publication)
    assert readiness["manual_status"] == "INCOMPLETE_SNAPSHOT"
    assert readiness["manual_ready"] is False


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("ACTIVE", "ACTIVE"),
        ("REVOKED", "AUTHORIZATION_REVOKED"),
        ("CONSUMED", "AUTHORIZATION_CONSUMED"),
        ("ACTIVE_EXPIRED", "AUTHORIZATION_EXPIRED"),
    ],
)
def test_readiness_reports_latest_authorization_status(status, expected):
    db = _db()
    publication = _ready_publication(db)
    auth = create_authorization(db, publication, actor="admin@example.test")
    if status == "REVOKED":
        auth.status = "REVOKED"
        auth.revoked_at = datetime.now(timezone.utc)
        auth.revoke_reason = "operator revoked"
    elif status == "CONSUMED":
        auth.status = "CONSUMED"
        auth.consumed_at = datetime.now(timezone.utc)
    elif status == "ACTIVE_EXPIRED":
        auth.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()

    readiness = readiness_result(db, publication)
    assert readiness["authorization"]["status"] == expected
    assert readiness["authorization"]["authorization_id"] == auth.id
    assert "quality_snapshot" not in readiness["authorization"]
    assert "readiness_snapshot" not in readiness["authorization"]
    assert "duplicate_snapshot" not in readiness["authorization"]


def test_readiness_reports_persisted_expired_status_without_mutation():
    db = _db()
    publication = _ready_publication(db)
    auth = create_authorization(db, publication, actor="admin@example.test")
    auth.status = "EXPIRED"
    db.commit()

    readiness = readiness_result(db, publication)
    assert readiness["authorization"] == {
        "status": "AUTHORIZATION_EXPIRED",
        "authorization_id": auth.id,
    }
    assert db.get(PublicationDispatchAuthorization, auth.id).status == "EXPIRED"


def test_integrity_error_classifier_is_narrowly_scoped():
    active = IntegrityError(
        "insert", {},
        Exception("UNIQUE constraint failed: publication_dispatch_authorizations.publication_id"),
    )
    assert _is_active_authorization_unique_violation(active)

    unrelated = IntegrityError("insert", {}, Exception("FOREIGN KEY constraint failed"))
    assert not _is_active_authorization_unique_violation(unrelated)


def test_integrity_error_classifier_accepts_postgres_constraint_identity():
    class Diag:
        constraint_name = "uq_publication_dispatch_authorizations_active"

    class Orig:
        diag = Diag()

    exc = IntegrityError("insert", {}, Orig())
    assert _is_active_authorization_unique_violation(exc)


def test_provider_readiness_requires_connected_connection_even_with_pins_write(monkeypatch):
    from app.services import publication_dispatch_authorization as auth_service

    db = _db()
    publication = _ready_publication(
        db,
        connection_scopes=["user_accounts:read", "boards:read", "pins:read", "pins:write"],
    )
    db.get(PinterestConnection, publication.pinterest_connection_id).status = "DISCONNECTED"
    db.commit()
    monkeypatch.setattr(auth_service, "get_settings", lambda: type("S", (), {"publishing_enabled": True})())

    readiness = provider_readiness(db, publication)
    assert readiness == {
        "status": "PUBLISHING_SCOPE_REQUIRED",
        "ready": False,
        "live_provider_write_enabled": False,
    }


def test_revoke_authorization_rolls_back_on_commit_failure():
    from app.services.publication_dispatch_authorization import revoke_authorization
    class DB:
        def __init__(self): self.rolled = False
        def add(self, obj): pass
        def commit(self): raise RuntimeError("db failure")
        def rollback(self): self.rolled = True
    db = DB()
    auth = _authorization(_publication(_db()), suffix="rollback")
    with pytest.raises(RuntimeError):
        revoke_authorization(db, auth, actor="admin", reason="operator")
    assert db.rolled


def test_two_session_authorization_creation_race_is_bounded(tmp_path, monkeypatch):
    from app.services import publication_dispatch_authorization as auth_service

    db_path = tmp_path / "auth-race.db"
    engine = create_engine(f"sqlite+pysqlite:///{db_path}")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    setup = SessionLocal()
    publication = _ready_publication(setup)
    setup.close()

    session_a = SessionLocal()
    session_b = SessionLocal()
    try:
        pub_a = session_a.get(PinPublication, publication.id)
        pub_b = session_b.get(PinPublication, publication.id)
        first = create_authorization(session_a, pub_a, actor="admin@example.test")
        assert first.status == "ACTIVE"
        monkeypatch.setattr(auth_service, "active_authorization", lambda *args, **kwargs: None)
        with pytest.raises(DispatchAuthorizationError, match="ACTIVE_AUTHORIZATION_EXISTS"):
            create_authorization(session_b, pub_b, actor="admin@example.test")

        audit = SessionLocal()
        try:
            active = (
                audit.query(PublicationDispatchAuthorization)
                .filter_by(publication_id=publication.id, status="ACTIVE")
                .all()
            )
            assert len(active) == 1
        finally:
            audit.close()
    finally:
        session_a.close()
        session_b.close()
        engine.dispose()
