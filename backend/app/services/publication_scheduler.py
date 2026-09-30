"""Explicit, bounded publication scheduling primitives (no background worker)."""
from datetime import datetime, timezone
from sqlalchemy import select, update
from app.models.domain import PinPublication, PublicationStatus, PublicationAttempt
from app.services.publication_state import require_transition
from app.services.fingerprints import _sha256
from app.services.local_canary_admission import pending_local_canary_creative_ids, publication_has_pending_local_canary_media

MAX_DISPATCH_BATCH = 25

def request_fingerprint(publication):
    return _sha256({"publication_fingerprint": publication.publication_fingerprint, "board_id": publication.pinterest_board_id_snapshot, "title": publication.title_snapshot, "description": publication.description_snapshot, "alt_text": publication.alt_text_snapshot, "link_url": publication.utm_url, "media_url": publication.media_url_snapshot})

def due_publications(db, *, now=None, limit=MAX_DISPATCH_BATCH):
    if limit < 1 or limit > MAX_DISPATCH_BATCH:
        raise ValueError("limit must be between 1 and 25")
    now = now or datetime.now(timezone.utc)
    query = select(PinPublication).where(
        PinPublication.status == PublicationStatus.SCHEDULED,
        PinPublication.scheduled_for <= now,
        PinPublication.creative_id.not_in(pending_local_canary_creative_ids()),
    ).order_by(PinPublication.scheduled_for, PinPublication.id)
    eligible = []
    target = min(limit, MAX_DISPATCH_BATCH)
    offset = 0
    while len(eligible) < target:
        candidates = db.scalars(
            query.limit(target).offset(offset)
        ).all()
        if not candidates:
            break
        offset += len(candidates)
        for publication in candidates:
            if publication_has_pending_local_canary_media(db, publication):
                continue
            eligible.append(publication)
            if len(eligible) >= target:
                break
    return eligible

def schedule(db, publication, when, *, commit=True):
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("scheduled_for must be timezone-aware")
    if publication.status == PublicationStatus.PUBLISH_UNKNOWN:
        raise ValueError("PUBLISH_UNKNOWN cannot be rescheduled")
    if publication.status not in {PublicationStatus.SCHEDULED, PublicationStatus.APPROVED, PublicationStatus.PUBLISH_FAILED}:
        require_transition(publication.status, PublicationStatus.SCHEDULED)
    when = when.astimezone(timezone.utc)
    if publication_has_pending_local_canary_media(db, publication):
        raise ValueError("LOCAL_CANARY_MEDIA_PENDING")
    publication.scheduled_for = when
    publication.status = PublicationStatus.SCHEDULED
    if commit:
        db.commit()
    else:
        db.flush()
    return publication

def cancel(db, publication):
    require_transition(publication.status, PublicationStatus.CANCELLED)
    publication.status = PublicationStatus.CANCELLED
    publication.scheduled_for = None
    db.commit()
    return publication

def claim(db, publication):
    """Compare-and-set claim; caller commits before provider execution."""
    if publication_has_pending_local_canary_media(db, publication):
        return None
    now = datetime.now(timezone.utc)
    result = db.execute(update(PinPublication).execution_options(synchronize_session=False).where(
        PinPublication.id == publication.id,
        PinPublication.status == PublicationStatus.SCHEDULED,
        PinPublication.creative_id.not_in(pending_local_canary_creative_ids()),
        PinPublication.scheduled_for.is_not(None),
        PinPublication.scheduled_for <= now,
    ).values(status=PublicationStatus.PUBLISHING, attempt_started_at=now))
    if result.rowcount != 1:
        return None
    attempt_no = (db.scalar(select(PublicationAttempt.attempt_number).where(PublicationAttempt.publication_id == publication.id).order_by(PublicationAttempt.attempt_number.desc()).limit(1)) or 0) + 1
    attempt = PublicationAttempt(publication_id=publication.id, attempt_number=attempt_no, status="STARTED", request_fingerprint=request_fingerprint_for(publication))
    db.add(attempt)
    db.commit()
    return attempt

request_fingerprint_for = request_fingerprint
