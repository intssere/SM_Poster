"""Explicit, bounded publication scheduling primitives (no background worker)."""
from datetime import datetime, timezone
from sqlalchemy import select
from app.models.domain import PinPublication, PublicationStatus
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

def claim(db, publication):
    """Compare-and-set claim; caller commits before provider execution."""
    if publication.status != PublicationStatus.SCHEDULED:
        return False
    require_transition(publication.status, PublicationStatus.PUBLISHING)
    publication.status = PublicationStatus.PUBLISHING
    publication.attempt_started_at = datetime.now(timezone.utc)
    db.flush()
    return True
