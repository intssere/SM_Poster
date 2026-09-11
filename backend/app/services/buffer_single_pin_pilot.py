"""Default-off, server-owned Buffer candidate gate. No provider traffic."""
from sqlalchemy import select
from app.models.domain import PublicationAttempt, PublicationStatus
from app.services.publication_scheduler import request_fingerprint_for


def validate_pilot(db, publication, settings, *, attempt=None):
    for flag, reason in (
        (settings.publishing_enabled, "PUBLISHING_DISABLED"),
        (settings.buffer_publishing_enabled, "BUFFER_PUBLISHING_DISABLED"),
        (settings.buffer_single_pin_pilot_enabled, "BUFFER_PILOT_DISABLED"),
    ):
        if flag is not True:
            return False, reason
    attempts = db.scalars(select(PublicationAttempt).where(PublicationAttempt.publication_id == publication.id)).all()
    if attempt is None:
        if attempts:
            return False, "BUFFER_PILOT_PRIOR_ATTEMPT"
    elif (len(attempts) != 1 or attempts[0].id != attempt.id or attempt.status != "STARTED"
          or attempt.dispatch_provider != "buffer" or publication.status != PublicationStatus.PUBLISHING
          or attempt.request_fingerprint != request_fingerprint_for(publication)):
        return False, "BUFFER_PILOT_ATTEMPT_MISMATCH"
    return True, "BUFFER_PILOT_READY"
