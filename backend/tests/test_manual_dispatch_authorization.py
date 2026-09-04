from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models.domain import (
    PinPublication,
    PublicationAttempt,
    PublicationDispatchAuthorization,
    PublicationReconciliationEvent,
    PublicationStatus,
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
