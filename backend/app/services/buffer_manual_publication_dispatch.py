"""Internal, explicitly invoked Buffer pilot. No API wiring or autonomous execution."""
from datetime import datetime, timezone
import re

from sqlalchemy import update
from app.core.config import get_settings
from app.integrations.buffer.gateway import (
    BufferGateway, BufferConfigurationError, BufferReadError,
    BufferDefinitiveRejection,
)
from app.models.domain import PinPublication, PublicationAttempt, PublicationStatus
from app.services.manual_publication_dispatch import atomic_authorized_claim, validate_post_claim, ManualDispatchError
from app.services.publication_dispatch_authorization import active_authorization, validate_authorization
from app.services.buffer_single_pin_pilot import validate_pilot
from app.services.buffer_pinterest_adapter import verify_destination, build_pinterest_payload
from app.services.buffer_publication_reconciliation import reconcile_buffer, BufferReconciliationError
from app.services.pinterest_publisher import PublicationReconciliationError, normalize_persisted_utc


def _configuration(settings):
    if (not isinstance(settings.buffer_api_key, str) or not settings.buffer_api_key.strip()
            or any(c.isspace() for c in settings.buffer_api_key)
            or not settings.buffer_organization_id or not settings.buffer_pinterest_channel_id
            or settings.buffer_api_base not in {"https://api.buffer.com", "https://api.buffer.com/"}):
        raise ManualDispatchError("BUFFER_CONFIGURATION_REQUIRED")
    if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", value)
           or settings.buffer_api_key in value for value in (settings.buffer_organization_id, settings.buffer_pinterest_channel_id)):
        raise ManualDispatchError("BUFFER_CONFIGURATION_REQUIRED")


def _persist(db, publication_id, attempt_id, status, code, settings, *, result=None, recovery=False):
    """One result transaction, at most one recovery transaction; never provider retry."""
    now = datetime.now(timezone.utc)
    terminal = status == PublicationStatus.PUBLISH_FAILED
    attempt_values = dict(status="FAILED" if terminal else "UNKNOWN", error_code=code,
                          completed_at=now if terminal or result is None else None,
                          safe_response_metadata={"buffer_channel_id": settings.buffer_pinterest_channel_id,
                              "buffer_organization_id": settings.buffer_organization_id})
    if result is not None:
        attempt_values.update(provider_operation_id=result.buffer_post_id, provider_operation_status=result.status,
                              provider_external_link=result.external_link, provider_submitted_at=now,
                              provider_last_observed_at=now)
    try:
        changed = db.execute(update(PinPublication).where(PinPublication.id == publication_id,
            PinPublication.status == PublicationStatus.PUBLISHING).values(status=status, error_code=code))
        if changed.rowcount != 1:
            raise PublicationReconciliationError("BUFFER_RESULT_STATE_CONFLICT")
        changed = db.execute(update(PublicationAttempt).where(PublicationAttempt.id == attempt_id,
            PublicationAttempt.publication_id == publication_id, PublicationAttempt.dispatch_provider == "buffer",
            PublicationAttempt.status == "STARTED").values(**attempt_values))
        if changed.rowcount != 1:
            raise PublicationReconciliationError("BUFFER_RESULT_STATE_CONFLICT")
        db.commit()
    except Exception:
        db.rollback()
        if result is not None and not recovery:
            _persist(db, publication_id, attempt_id, PublicationStatus.PUBLISH_UNKNOWN,
                     "BUFFER_STATE_PERSISTENCE_UNKNOWN", settings, result=result, recovery=True)
            return False
        raise PublicationReconciliationError("BUFFER_RECONCILIATION_PERSISTENCE_FAILED") from None
    return True


async def dispatch_buffer(db, publication, *, now=None, settings=None, gateway=None):
    settings = settings or get_settings()
    now = normalize_persisted_utc(now or datetime.now(timezone.utc))
    authorization = active_authorization(db, publication.id)
    validated = validate_authorization(db, publication, authorization, now=now)
    if not validated["valid"]:
        raise ManualDispatchError(validated["status"])
    ok, reason = validate_pilot(db, publication, settings)
    if not ok:
        raise ManualDispatchError(reason)
    _configuration(settings)
    gateway = gateway or BufferGateway(settings)
    try:
        await verify_destination(publication, settings, gateway)
        payload = build_pinterest_payload(publication, settings)
    except (BufferConfigurationError, BufferReadError):
        raise ManualDispatchError("BUFFER_PRECLAIM_VALIDATION_FAILED") from None
    record_settings = settings.model_copy(deep=True)
    attempt = atomic_authorized_claim(db, publication, authorization, now=now, dispatch_provider="buffer")
    if attempt is None:
        raise ManualDispatchError("AUTHORIZED_CLAIM_FAILED")
    publication_id, attempt_id, actor = publication.id, attempt.id, authorization.authorized_by
    try:
        db.refresh(publication)
        db.refresh(authorization)
        db.refresh(attempt)
        if not validate_post_claim(db, publication, authorization, attempt, now=now)["valid"]:
            raise ManualDispatchError("BUFFER_POSTCLAIM_VALIDATION_FAILED")
        ok, reason = validate_pilot(db, publication, settings, attempt=attempt)
        if not ok:
            raise ManualDispatchError(reason)
        _configuration(settings)
        await verify_destination(publication, settings, gateway)
        # Revalidate DB identity after the last read, immediately before mutation.
        db.refresh(publication)
        db.refresh(authorization)
        db.refresh(attempt)
        if not validate_post_claim(db, publication, authorization, attempt, now=now)["valid"]:
            raise ManualDispatchError("BUFFER_POSTCLAIM_VALIDATION_FAILED")
        ok, reason = validate_pilot(db, publication, settings, attempt=attempt)
        if not ok:
            raise ManualDispatchError(reason)
        payload = build_pinterest_payload(publication, settings)
        _configuration(settings)
    except Exception:
        db.rollback()
        _persist(db, publication_id, attempt_id, PublicationStatus.PUBLISH_FAILED,
                 "BUFFER_POSTCLAIM_VALIDATION_FAILED", record_settings)
        raise ManualDispatchError("BUFFER_POSTCLAIM_VALIDATION_FAILED") from None
    result = None
    try:
        result = await gateway.create_pinterest_post(payload)
    except BufferDefinitiveRejection:
        status, code = PublicationStatus.PUBLISH_FAILED, "PROVIDER_REJECTED"
    except Exception:
        # Any unexpected exception at the mutation boundary is uncertain, not retryable.
        status, code = PublicationStatus.PUBLISH_UNKNOWN, "PUBLISH_UNKNOWN"
    else:
        status = PublicationStatus.PUBLISH_FAILED if result.status == "error" else PublicationStatus.PUBLISH_UNKNOWN
        code = "BUFFER_PROVIDER_FAILED" if result.status == "error" else "BUFFER_FINAL_OUTCOME_PENDING"
    persisted = _persist(db, publication_id, attempt_id, status, code, record_settings, result=result)
    if persisted and result is not None and result.status == "sent":
        try:
            await reconcile_buffer(db, publication_id, actor=actor, settings=settings, gateway=gateway)
        except BufferReconciliationError:
            # No fuzzy success. Keep known operation for a later explicit observation.
            try:
                db.execute(update(PinPublication).where(PinPublication.id == publication_id,
                    PinPublication.status == PublicationStatus.PUBLISH_UNKNOWN).values(error_code="BUFFER_SENT_LINK_UNVERIFIED"))
                db.execute(update(PublicationAttempt).where(PublicationAttempt.id == attempt_id,
                    PublicationAttempt.status == "UNKNOWN").values(error_code="BUFFER_SENT_LINK_UNVERIFIED"))
                db.commit()
            except Exception:
                db.rollback()
                raise PublicationReconciliationError("BUFFER_RECONCILIATION_PERSISTENCE_FAILED") from None
    db.refresh(publication)
    return publication
