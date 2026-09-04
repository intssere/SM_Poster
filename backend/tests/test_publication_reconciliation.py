import pytest
from types import SimpleNamespace
from copy import copy
from sqlalchemy.orm import make_transient

from app.services.publication_reconciliation import ReconciliationError, _pin, reconcile
from app.models.domain import PublicationStatus, PublicationReconciliationEvent, PublicationAttempt
from test_manual_dispatch_authorization import _db, _ready_publication


@pytest.mark.parametrize("value", ["", " ", "a b", "https://x", "a/b", "a!", "x\n", "x" * 256])
def test_provider_pin_ids_reject_unsafe_values(value):
    with pytest.raises(ReconciliationError, match="INVALID_PROVIDER_PIN_ID"):
        _pin(value)


def test_provider_pin_ids_accept_token_style_values():
    assert _pin("Pin-01_v2.example:abc") == "Pin-01_v2.example:abc"
    assert len(_pin("a" * 80)) == 80
    with pytest.raises(ReconciliationError, match="INVALID_PROVIDER_PIN_ID"):
        _pin("a" * 81)


def test_reconcile_rejects_confirmation_and_unsupported_action():
    with pytest.raises(ReconciliationError, match="CONFIRMATION_REQUIRED"):
        reconcile(SimpleNamespace(), "missing", actor="admin", action="PROVIDER_PIN_CONFIRMED", confirmed=False)
    with pytest.raises(ReconciliationError, match="RECONCILIATION_REQUIRES_PUBLISH_UNKNOWN"):
        reconcile(SimpleNamespace(get=lambda *_: None), "missing", actor="admin", action="NOPE", confirmed=True)


def test_reconcile_requires_publish_unknown():
    db = SimpleNamespace(get=lambda *_: SimpleNamespace(status="SCHEDULED"))
    with pytest.raises(ReconciliationError, match="RECONCILIATION_REQUIRES_PUBLISH_UNKNOWN"):
        reconcile(db, "p", actor="admin", action="CANCELLED_UNKNOWN", confirmed=True, reason="stop")


def test_provider_pin_confirmation_persists_real_publication_and_event():
    db = _db()
    publication = _ready_publication(db, status=PublicationStatus.PUBLISH_UNKNOWN)
    reconcile(db, publication.id, actor="admin@example.test", action="PROVIDER_PIN_CONFIRMED", confirmed=True, provider_pin_id="pin-123")
    db.refresh(publication)
    assert publication.status == PublicationStatus.PUBLISHED
    assert publication.pinterest_pin_id == "pin-123"
    event = db.query(PublicationReconciliationEvent).filter_by(publication_id=publication.id).one()
    assert (event.actor, event.previous_status, event.new_status, event.provider_pin_id) == ("admin@example.test", "PUBLISH_UNKNOWN", "PUBLISHED", "pin-123")


def test_cancelled_unknown_persists_event_and_clears_schedule():
    db = _db()
    publication = _ready_publication(db, status=PublicationStatus.PUBLISH_UNKNOWN)
    reconcile(db, publication.id, actor="admin@example.test", action="CANCELLED_UNKNOWN", confirmed=True, reason="Operator confirmed cancellation")
    db.refresh(publication)
    assert publication.status == PublicationStatus.CANCELLED
    assert publication.scheduled_for is None
    assert db.query(PublicationReconciliationEvent).count() == 1


def test_conflicting_known_pin_ids_fail_closed_without_event():
    db = _db()
    publication = _ready_publication(db, status=PublicationStatus.PUBLISH_UNKNOWN)
    publication.pinterest_pin_id = "pin-a"
    db.add(PublicationAttempt(publication_id=publication.id, attempt_number=1, status="UNKNOWN", provider_pin_id="pin-b"))
    db.commit()
    with pytest.raises(ReconciliationError, match="CONFLICTING_KNOWN_PROVIDER_PIN_IDS"):
        reconcile(db, publication.id, actor="admin@example.test", action="PROVIDER_PIN_CONFIRMED", confirmed=True, provider_pin_id="pin-a")
    assert db.query(PublicationReconciliationEvent).count() == 0


def test_invalid_reason_is_rejected_before_state_transition():
    db = _db()
    publication = _ready_publication(db, status=PublicationStatus.PUBLISH_UNKNOWN)
    with pytest.raises(ReconciliationError, match="INVALID_REASON"):
        reconcile(db, publication.id, actor="admin@example.test", action="PROVIDER_PIN_CONFIRMED", confirmed=True, provider_pin_id="pin-123", reason="bad\x7freason")
    db.refresh(publication)
    assert publication.status == PublicationStatus.PUBLISH_UNKNOWN
    assert publication.pinterest_pin_id is None
    assert db.query(PublicationReconciliationEvent).count() == 0


@pytest.mark.parametrize("kind", ["publication", "attempt"])
def test_known_or_duplicate_pin_ids_are_rejected_without_mutation(kind):
    db = _db(); publication = _ready_publication(db, status=PublicationStatus.PUBLISH_UNKNOWN)
    if kind == "publication":
        publication.pinterest_pin_id = "pin-known"; submitted = "pin-other"
    elif kind == "attempt":
        db.add(PublicationAttempt(publication_id=publication.id, attempt_number=1, status="UNKNOWN", provider_pin_id="pin-known")); submitted = "pin-other"
    db.commit()
    expected = "KNOWN_PROVIDER_PIN_MISMATCH" if kind in {"publication", "attempt"} else "PROVIDER_PIN_ID_ALREADY_ASSIGNED"
    with pytest.raises(ReconciliationError, match=expected):
        reconcile(db, publication.id, actor="admin", action="PROVIDER_PIN_CONFIRMED", confirmed=True, provider_pin_id=submitted)
    db.refresh(publication)
    assert publication.status == PublicationStatus.PUBLISH_UNKNOWN
    assert db.query(PublicationReconciliationEvent).count() == 0


def test_known_pin_blocks_unknown_cancellation():
    db = _db(); publication = _ready_publication(db, status=PublicationStatus.PUBLISH_UNKNOWN)
    publication.pinterest_pin_id = "pin-known"; db.commit()
    with pytest.raises(ReconciliationError, match="KNOWN_PROVIDER_PIN_REQUIRES_CONFIRMATION"):
        reconcile(db, publication.id, actor="admin", action="CANCELLED_UNKNOWN", confirmed=True, reason="stop")


def test_duplicate_pin_on_another_publication_is_rejected():
    db = _db(); target = _ready_publication(db, status=PublicationStatus.PUBLISH_UNKNOWN)
    from app.models.domain import PinPublication
    other = PinPublication(id="other-pub", draft_id=target.draft_id, revision_id=target.revision_id, creative_id=target.creative_id, approval_id=target.approval_id, source_image_id=target.source_image_id, template_id=target.template_id, template_key=target.template_key, template_version=target.template_version, text_fingerprint=target.text_fingerprint, creative_fingerprint=target.creative_fingerprint, pinterest_connection_id=target.pinterest_connection_id, pinterest_board_record_id=target.pinterest_board_record_id, pinterest_board_id_snapshot=target.pinterest_board_id_snapshot, title_snapshot=target.title_snapshot, description_snapshot=target.description_snapshot, alt_text_snapshot=target.alt_text_snapshot, media_url_snapshot=target.media_url_snapshot, destination_url=target.destination_url, utm_url=target.utm_url, publication_fingerprint="o"*64, status=PublicationStatus.PUBLISHED, pinterest_pin_id="pin-dup")
    db.add(other); db.commit()
    with pytest.raises(ReconciliationError, match="PROVIDER_PIN_ID_ALREADY_ASSIGNED"):
        reconcile(db, target.id, actor="admin", action="PROVIDER_PIN_CONFIRMED", confirmed=True, provider_pin_id="pin-dup")
    assert db.query(PublicationReconciliationEvent).count() == 0


def test_duplicate_pin_on_another_attempt_is_rejected():
    db = _db(); target = _ready_publication(db, status=PublicationStatus.PUBLISH_UNKNOWN)
    from app.models.domain import PinPublication
    other = PinPublication(id="other-pub", draft_id=target.draft_id, revision_id=target.revision_id, creative_id=target.creative_id, approval_id=target.approval_id, source_image_id=target.source_image_id, template_id=target.template_id, template_key=target.template_key, template_version=target.template_version, text_fingerprint=target.text_fingerprint, creative_fingerprint=target.creative_fingerprint, pinterest_connection_id=target.pinterest_connection_id, pinterest_board_record_id=target.pinterest_board_record_id, pinterest_board_id_snapshot=target.pinterest_board_id_snapshot, title_snapshot=target.title_snapshot, description_snapshot=target.description_snapshot, alt_text_snapshot=target.alt_text_snapshot, media_url_snapshot=target.media_url_snapshot, destination_url=target.destination_url, utm_url=target.utm_url, publication_fingerprint="o"*64, status=PublicationStatus.PUBLISHED)
    db.add(other); db.flush(); db.add(PublicationAttempt(publication_id=other.id, attempt_number=1, status="UNKNOWN", provider_pin_id="pin-dup")); db.commit()
    with pytest.raises(ReconciliationError, match="PROVIDER_PIN_ID_ALREADY_ASSIGNED"):
        reconcile(db, target.id, actor="admin", action="PROVIDER_PIN_CONFIRMED", confirmed=True, provider_pin_id="pin-dup")
    assert db.query(PublicationReconciliationEvent).count() == 0
