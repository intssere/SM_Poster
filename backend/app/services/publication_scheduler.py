"""Explicit, bounded publication scheduling primitives (no background worker)."""
from datetime import datetime, timezone
from sqlalchemy import select, update
from app.models.domain import PinPublication, PublicationStatus, PublicationAttempt
from app.services.publication_state import require_transition

MAX_DISPATCH_BATCH = 25

def due_publications(db, *, now=None, limit=MAX_DISPATCH_BATCH):
    now = now or datetime.now(timezone.utc)
    return db.scalars(select(PinPublication).where(
        PinPublication.status == PublicationStatus.SCHEDULED,
        PinPublication.scheduled_for <= now,
    ).order_by(PinPublication.scheduled_for, PinPublication.id).limit(min(limit, MAX_DISPATCH_BATCH))).all()

def schedule(db, publication, when):
    require_transition(publication.status, PublicationStatus.SCHEDULED)
    publication.scheduled_for = when
    publication.status = PublicationStatus.SCHEDULED
    db.commit()
    return publication

def cancel(db, publication):
    require_transition(publication.status, PublicationStatus.CANCELLED)
    publication.status = PublicationStatus.CANCELLED
    publication.scheduled_for = None
    db.commit()
    return publication

def claim(db, publication, request_fingerprint=None):
    """Compare-and-set claim; caller commits before provider execution."""
    now = datetime.now(timezone.utc)
    result = db.execute(update(PinPublication).where(
        PinPublication.id == publication.id,
        PinPublication.status == PublicationStatus.SCHEDULED,
        PinPublication.scheduled_for.is_not(None),
        PinPublication.scheduled_for <= now,
    ).values(status=PublicationStatus.PUBLISHING, attempt_started_at=now))
    if result.rowcount != 1:
        return None
    attempt_no = (db.scalar(select(PublicationAttempt.attempt_number).where(PublicationAttempt.publication_id == publication.id).order_by(PublicationAttempt.attempt_number.desc()).limit(1)) or 0) + 1
    attempt = PublicationAttempt(publication_id=publication.id, attempt_number=attempt_no, status="STARTED", request_fingerprint=request_fingerprint)
    db.add(attempt)
    db.commit()
    return attempt
