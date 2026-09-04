"""Server-side, fail-closed candidate gate for the future single-Pin pilot."""
from sqlalchemy import select
from app.core.config import get_settings
from app.models.domain import PinPublication, PublicationAttempt
from app.services.publication_scheduler import request_fingerprint_for

def validate_pilot_candidate(db, publication: PinPublication) -> tuple[bool, str]:
    settings = get_settings()
    if not settings.publishing_enabled:
        return False, "PILOT_DISABLED"
    if not settings.pinterest_single_pin_pilot_enabled:
        return False, "PILOT_DISABLED"
    if not settings.pinterest_single_pin_pilot_publication_id or not settings.pinterest_single_pin_pilot_publication_fingerprint or not settings.pinterest_single_pin_pilot_request_fingerprint:
        return False, "PILOT_BINDING_INCOMPLETE"
    if settings.pinterest_single_pin_pilot_publication_id != publication.id:
        return False, "PILOT_PUBLICATION_MISMATCH"
    if settings.pinterest_single_pin_pilot_publication_fingerprint != publication.publication_fingerprint:
        return False, "PILOT_PUBLICATION_FINGERPRINT_MISMATCH"
    if settings.pinterest_single_pin_pilot_request_fingerprint != request_fingerprint_for(publication):
        return False, "PILOT_REQUEST_FINGERPRINT_MISMATCH"
    if db.scalar(select(PublicationAttempt.id).where(PublicationAttempt.publication_id == publication.id).limit(1)):
        return False, "PILOT_ALREADY_ATTEMPTED"
    return True, "READY"

def validate_post_claim_pilot(db, publication, attempt) -> tuple[bool, str]:
    settings = get_settings()
    if not settings.publishing_enabled or not settings.pinterest_single_pin_pilot_enabled:
        return False, "PILOT_DISABLED"
    if settings.pinterest_single_pin_pilot_publication_id != publication.id or settings.pinterest_single_pin_pilot_publication_fingerprint != publication.publication_fingerprint or settings.pinterest_single_pin_pilot_request_fingerprint != request_fingerprint_for(publication):
        return False, "PILOT_BINDING_MISMATCH"
    attempts = db.scalars(select(PublicationAttempt).where(PublicationAttempt.publication_id == publication.id)).all()
    if len(attempts) != 1 or attempts[0].id != attempt.id or attempt.status != "STARTED" or publication.status.value != "PUBLISHING":
        return False, "PILOT_POST_CLAIM_INVALID"
    if attempt.request_fingerprint != request_fingerprint_for(publication):
        return False, "PILOT_REQUEST_FINGERPRINT_MISMATCH"
    return True, "READY"
