from __future__ import annotations
import re
from datetime import datetime, timezone
from sqlalchemy import select, update
from app.models.domain import PinPublication, PublicationAttempt, PublicationReconciliationEvent, PublicationStatus, AuditLog

class ReconciliationError(RuntimeError): pass
MAX_PROVIDER_PIN_ID_LENGTH = 80
_PIN = re.compile(rf"^[A-Za-z0-9._:-]{{1,{MAX_PROVIDER_PIN_ID_LENGTH}}}$")

def _validate_reason(reason: str | None, *, required: bool = False) -> str | None:
    if reason is None:
        if required:
            raise ReconciliationError("REASON_REQUIRED")
        return None
    if len(reason) > 500 or not reason.strip() or any(ord(c) < 32 or ord(c) == 127 for c in reason):
        raise ReconciliationError("REASON_REQUIRED" if required else "INVALID_REASON")
    return reason

def _pin(value):
    if not isinstance(value, str) or not _PIN.fullmatch(value) or any(c.isspace() or ord(c) < 32 for c in value):
        raise ReconciliationError("INVALID_PROVIDER_PIN_ID")
    return value

def reconcile(db, publication_id: str, *, actor: str, action: str, confirmed: bool, provider_pin_id: str | None = None, reason: str | None = None):
    if not actor: raise ReconciliationError("ACTOR_REQUIRED")
    if not confirmed: raise ReconciliationError("CONFIRMATION_REQUIRED")
    publication = db.get(PinPublication, publication_id)
    if not publication or publication.status != PublicationStatus.PUBLISH_UNKNOWN: raise ReconciliationError("RECONCILIATION_REQUIRES_PUBLISH_UNKNOWN")
    attempts = db.scalars(select(PublicationAttempt).where(PublicationAttempt.publication_id == publication_id)).all()
    known = {a.provider_pin_id for a in attempts if a.provider_pin_id}
    if publication.pinterest_pin_id: known.add(publication.pinterest_pin_id)
    if len(known) > 1: raise ReconciliationError("CONFLICTING_KNOWN_PROVIDER_PIN_IDS")
    if action == "PROVIDER_PIN_CONFIRMED":
        pin = _pin(provider_pin_id)
        reason = _validate_reason(reason)
        if known and pin not in known: raise ReconciliationError("KNOWN_PROVIDER_PIN_MISMATCH")
        other = db.scalar(select(PinPublication).where(PinPublication.pinterest_pin_id == pin, PinPublication.id != publication_id))
        if other: raise ReconciliationError("PROVIDER_PIN_ID_ALREADY_ASSIGNED")
        other_attempt = db.scalar(select(PublicationAttempt).where(PublicationAttempt.provider_pin_id == pin, PublicationAttempt.publication_id != publication_id))
        if other_attempt: raise ReconciliationError("PROVIDER_PIN_ID_ALREADY_ASSIGNED")
        new_status, event_pin = PublicationStatus.PUBLISHED, pin
    elif action == "CANCELLED_UNKNOWN":
        reason = _validate_reason(reason, required=True)
        if known: raise ReconciliationError("KNOWN_PROVIDER_PIN_REQUIRES_CONFIRMATION")
        new_status, event_pin = PublicationStatus.CANCELLED, None
    else: raise ReconciliationError("UNSUPPORTED_RECONCILIATION_ACTION")
    now = datetime.now(timezone.utc)
    claimed = db.execute(update(PinPublication).where(PinPublication.id == publication_id, PinPublication.status == PublicationStatus.PUBLISH_UNKNOWN).values(status=new_status, pinterest_pin_id=event_pin, published_at=now if event_pin else None, scheduled_for=None if not event_pin else publication.scheduled_for, error_code=None)).rowcount
    if claimed != 1: db.rollback(); raise ReconciliationError("RECONCILIATION_CONFLICT")
    reason = _validate_reason(reason)
    event = PublicationReconciliationEvent(publication_id=publication_id, attempt_id=next((a.id for a in attempts if a.status == "UNKNOWN"), None), actor=actor, action=action, previous_status="PUBLISH_UNKNOWN", new_status=new_status.value, provider_pin_id=event_pin, reason=reason)
    db.add(event)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise ReconciliationError("RECONCILIATION_PERSISTENCE_FAILED") from None
    return publication
