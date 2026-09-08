"""Explicit exact-post observation only. No polling, mutation or automatic retry."""
from datetime import datetime, timezone
import re
from urllib.parse import urlsplit

from sqlalchemy import select, update
from app.core.config import get_settings
from app.integrations.buffer.gateway import BufferGateway, public_url, BufferReadError, BufferConfigurationError
from app.models.domain import Board, PinConcept, PinDraft, PinPublication, PublicationAttempt, PublicationReconciliationEvent, PublicationStatus
from app.services.publication_scheduler import request_fingerprint_for
from app.services.pinterest_publisher import PublicationReconciliationError


class BufferReconciliationError(RuntimeError):
    """A bounded failure; never contains a provider body or credential."""


def pinterest_pin_id(link):
    try:
        scheme, host, port, path = public_url(link)
        parsed = urlsplit(link)
        match = re.fullmatch(r"/pin/([0-9]{1,80})/?", path)
        if host not in {"pinterest.com", "www.pinterest.com"} or port != 443 or parsed.query or parsed.fragment or not match:
            return None
        return match.group(1)
    except (ValueError, TypeError, BufferConfigurationError):
        return None


def snapshot_matches(publication, snapshot, settings):
    return all((expected and expected == observed) for expected, observed in (
        (settings.buffer_pinterest_channel_id, snapshot.channel_id),
        ("pinterest", snapshot.channel_service),
        (publication.description_snapshot, snapshot.text),
        (publication.pinterest_board_id_snapshot, snapshot.pinterest_board_service_id),
        (publication.title_snapshot, snapshot.pinterest_title),
        (publication.utm_url, snapshot.pinterest_url),
        (publication.media_url_snapshot, snapshot.image_url),
        (publication.alt_text_snapshot, snapshot.image_alt_text),
    ))


def _entry(db, publication_id, settings):
    publication = db.get(PinPublication, publication_id, populate_existing=True)
    if not publication or publication.status != PublicationStatus.PUBLISH_UNKNOWN:
        raise BufferReconciliationError("RECONCILIATION_REQUIRES_PUBLISH_UNKNOWN")
    attempts = db.scalars(select(PublicationAttempt).where(PublicationAttempt.publication_id == publication_id)
                          .execution_options(populate_existing=True)).all()
    operations = {a.provider_operation_id for a in attempts if a.provider_operation_id}
    if not operations:
        raise BufferReconciliationError("BUFFER_OPERATION_ID_REQUIRED")
    buffers = [a for a in attempts if a.dispatch_provider == "buffer"]
    if len(operations) != 1 or len(buffers) != 1 or not buffers[0].provider_operation_id or buffers[0].status != "UNKNOWN":
        raise BufferReconciliationError("BUFFER_OPERATION_CONFLICT")
    attempt = buffers[0]
    if db.scalar(select(PublicationAttempt.id).where(PublicationAttempt.dispatch_provider == "buffer",
            PublicationAttempt.provider_operation_id == attempt.provider_operation_id, PublicationAttempt.id != attempt.id)):
        raise BufferReconciliationError("BUFFER_OPERATION_CONFLICT")
    known = {a.provider_pin_id for a in attempts if a.provider_pin_id}
    if publication.pinterest_pin_id:
        known.add(publication.pinterest_pin_id)
    if len(known) > 1:
        raise BufferReconciliationError("CONFLICTING_KNOWN_PROVIDER_PIN_IDS")
    metadata = attempt.safe_response_metadata or {}
    board = db.get(Board, publication.board_id) if publication.board_id else None
    draft = db.get(PinDraft, publication.draft_id) if publication.draft_id else None
    concept = db.get(PinConcept, draft.concept_id) if draft else None
    buffer_identity_valid = (
        board is not None and board.active and bool(board.pinterest_board_id)
        and publication.pinterest_connection_id is None
        and publication.pinterest_board_record_id is None
        and bool(publication.pinterest_board_id_snapshot)
        and board.pinterest_board_id == publication.pinterest_board_id_snapshot
        and concept is not None and concept.board_id == publication.board_id
        and concept.store_id == board.store_id
    )
    if (not publication.publication_fingerprint or not publication.creative_id or not publication.revision_id
            or not buffer_identity_valid
            or attempt.request_fingerprint != request_fingerprint_for(publication)
            or not settings.buffer_organization_id or not settings.buffer_pinterest_channel_id
            or metadata.get("buffer_organization_id") != settings.buffer_organization_id
            or metadata.get("buffer_channel_id") != settings.buffer_pinterest_channel_id):
        raise BufferReconciliationError("BUFFER_POST_SNAPSHOT_MISMATCH")
    return publication, attempt, known


async def reconcile_buffer(db, publication_id, *, actor, settings=None, gateway=None):
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 255 or any(ord(c) < 32 for c in actor):
        raise BufferReconciliationError("ACTOR_REQUIRED")
    settings = settings or get_settings()
    publication, attempt, known = _entry(db, publication_id, settings)
    operation_id = attempt.provider_operation_id
    expected_request = attempt.request_fingerprint
    gateway = gateway or BufferGateway(settings)
    try:
        snapshot = await gateway.post(operation_id)
    except (BufferReadError, BufferConfigurationError):
        raise BufferReconciliationError("BUFFER_RECONCILIATION_READ_FAILED") from None
    # Re-read after I/O; no stale ORM state may authorize a terminal transition.
    publication, attempt, known = _entry(db, publication_id, settings)
    if operation_id != attempt.provider_operation_id or expected_request != attempt.request_fingerprint:
        raise BufferReconciliationError("BUFFER_OPERATION_CONFLICT")
    if snapshot.buffer_post_id != operation_id or not snapshot_matches(publication, snapshot, settings):
        raise BufferReconciliationError("BUFFER_POST_SNAPSHOT_MISMATCH")
    pin = pinterest_pin_id(snapshot.external_link) if snapshot.status == "sent" else None
    if snapshot.status == "sent" and not pin:
        raise BufferReconciliationError("BUFFER_SENT_LINK_UNVERIFIED")
    if pin:
        if known and known != {pin}:
            raise BufferReconciliationError("KNOWN_PROVIDER_PIN_MISMATCH")
        if (db.scalar(select(PinPublication.id).where(PinPublication.pinterest_pin_id == pin, PinPublication.id != publication_id))
                or db.scalar(select(PublicationAttempt.id).where(PublicationAttempt.provider_pin_id == pin, PublicationAttempt.publication_id != publication_id))):
            raise BufferReconciliationError("PROVIDER_PIN_ID_ALREADY_ASSIGNED")
    elif known and snapshot.status == "error":
        raise BufferReconciliationError("KNOWN_PROVIDER_PIN_REQUIRES_CONFIRMATION")
    now = datetime.now(timezone.utc)
    terminal = snapshot.status in {"sent", "error"}
    status = PublicationStatus.PUBLISHED if pin else PublicationStatus.PUBLISH_FAILED if terminal else PublicationStatus.PUBLISH_UNKNOWN
    code = None if pin else "BUFFER_PROVIDER_FAILED_CONFIRMED" if terminal else "BUFFER_FINAL_OUTCOME_PENDING"
    try:
        # The publication CAS serializes observers; attempt CAS and audit share its transaction.
        result = db.execute(update(PinPublication).where(PinPublication.id == publication_id,
            PinPublication.status == PublicationStatus.PUBLISH_UNKNOWN,
            PinPublication.publication_fingerprint == publication.publication_fingerprint).values(
                status=status, error_code=code, pinterest_pin_id=pin if terminal else publication.pinterest_pin_id,
                published_at=now if pin else publication.published_at))
        if result.rowcount != 1:
            raise BufferReconciliationError("RECONCILIATION_CONFLICT")
        values = dict(provider_operation_status=snapshot.status, provider_external_link=snapshot.external_link,
                      provider_last_observed_at=now, error_code=code)
        if terminal:
            values.update(status="SUCCEEDED" if pin else "FAILED", completed_at=now, provider_pin_id=pin)
        result = db.execute(update(PublicationAttempt).where(PublicationAttempt.id == attempt.id,
            PublicationAttempt.status == "UNKNOWN", PublicationAttempt.provider_operation_id == operation_id,
            PublicationAttempt.request_fingerprint == expected_request).values(**values))
        if result.rowcount != 1:
            raise BufferReconciliationError("RECONCILIATION_CONFLICT")
        if terminal:
            db.add(PublicationReconciliationEvent(publication_id=publication_id, attempt_id=attempt.id,
                actor=actor, action="PROVIDER_PIN_CONFIRMED" if pin else "PROVIDER_FAILURE_CONFIRMED",
                previous_status="PUBLISH_UNKNOWN", new_status=status.value, provider="buffer",
                provider_operation_id=operation_id, provider_operation_status=snapshot.status,
                provider_pin_id=pin, reason="Exact Buffer post and immutable snapshot verified"))
        db.commit()
    except BufferReconciliationError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise PublicationReconciliationError("BUFFER_RECONCILIATION_PERSISTENCE_FAILED") from None
    db.refresh(publication)
    return publication
