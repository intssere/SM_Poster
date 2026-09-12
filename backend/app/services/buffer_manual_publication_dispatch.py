"""Internal, explicitly invoked Buffer pilot. No API wiring or autonomous execution."""
from datetime import datetime, timezone
import logging
import re

from sqlalchemy import update
from app.core.config import get_settings
from app.integrations.buffer.gateway import (
    BufferGateway, BufferConfigurationError, BufferReadError,
    BufferDefinitiveRejection, BufferAmbiguousFailure,
)
from app.models.domain import PinPublication, PublicationAttempt, PublicationStatus
from app.services.manual_publication_dispatch import atomic_authorized_claim, validate_post_claim, ManualDispatchError
from app.services.publication_dispatch_authorization import active_authorization, validate_authorization
from app.services.buffer_single_pin_pilot import validate_pilot
from app.services.buffer_pinterest_adapter import verify_destination, build_pinterest_payload
from app.services.buffer_publication_reconciliation import reconcile_buffer, BufferReconciliationError
from app.services.pinterest_publisher import PublicationReconciliationError, normalize_persisted_utc
from app.services.buffer_pilot_execution_gate import (
    BufferPilotExecutionEvidence, FINAL_EXECUTION_READY,
    evaluate_buffer_pilot_execution_readiness,
)
from app.services.buffer_pilot_activation import active_activation, validate_activation


logger = logging.getLogger(__name__)


def _configuration(settings):
    if (not isinstance(settings.buffer_api_key, str) or not settings.buffer_api_key.strip()
            or any(c.isspace() for c in settings.buffer_api_key)
            or not settings.buffer_organization_id or not settings.buffer_pinterest_channel_id
            or settings.buffer_api_base not in {"https://api.buffer.com", "https://api.buffer.com/"}):
        raise ManualDispatchError("BUFFER_CONFIGURATION_REQUIRED")
    if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", value)
           or settings.buffer_api_key in value for value in (settings.buffer_organization_id, settings.buffer_pinterest_channel_id)):
        raise ManualDispatchError("BUFFER_CONFIGURATION_REQUIRED")


def _unexpected_diagnostic():
    return {
        "outcome_class": "ambiguous",
        "failure_code": "unexpected_exception",
        "phase": "mutation_boundary",
        "response_received": None,
        "http_status": None,
        "request_send_state": "unknown",
    }


def _log_mutation_outcome(publication_id, attempt_id, *, diagnostic=None, result=None):
    diagnostic = diagnostic or {}
    logger.info(
        "buffer_mutation_outcome",
        extra={
            "buffer_publication_id": publication_id,
            "buffer_attempt_id": attempt_id,
            "buffer_provider": "buffer",
            "buffer_outcome_class": diagnostic.get("outcome_class", "provider_result" if result is not None else "unknown"),
            "buffer_failure_code": diagnostic.get("failure_code"),
            "buffer_failure_phase": diagnostic.get("phase"),
            "buffer_http_status": diagnostic.get("http_status"),
            "buffer_provider_operation_id": result.buffer_post_id if result is not None else None,
        },
    )


def _persist(db, publication_id, attempt_id, status, code, settings, *, result=None,
             mutation_diagnostic=None, recovery=False):
    """One result transaction, at most one recovery transaction; never provider retry."""
    now = datetime.now(timezone.utc)
    terminal = status == PublicationStatus.PUBLISH_FAILED
    safe_metadata = {
        "buffer_channel_id": settings.buffer_pinterest_channel_id,
        "buffer_organization_id": settings.buffer_organization_id,
    }
    if mutation_diagnostic is not None:
        safe_metadata["mutation"] = {
            "outcome_class": mutation_diagnostic.get("outcome_class"),
            "failure_code": mutation_diagnostic.get("failure_code"),
            "phase": mutation_diagnostic.get("phase"),
            "response_received": mutation_diagnostic.get("response_received"),
            "http_status": mutation_diagnostic.get("http_status"),
            "request_send_state": mutation_diagnostic.get("request_send_state", "unknown"),
            "observed_at": now.isoformat(),
        }
    attempt_values = dict(status="FAILED" if terminal else "UNKNOWN", error_code=code,
                          completed_at=now if terminal or result is None else None,
                          safe_response_metadata=safe_metadata)
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
                     "BUFFER_STATE_PERSISTENCE_UNKNOWN", settings, result=result,
                     mutation_diagnostic=mutation_diagnostic, recovery=True)
            return False
        raise PublicationReconciliationError("BUFFER_RECONCILIATION_PERSISTENCE_FAILED") from None
    return True


async def dispatch_buffer(db, publication, *, now=None, settings=None, gateway=None,
                          execution_evidence: BufferPilotExecutionEvidence | None = None):
    settings = settings or get_settings()
    now = normalize_persisted_utc(now or datetime.now(timezone.utc))
    with db.no_autoflush:
        authorization = active_authorization(db, publication.id)
        activation = active_activation(db)
        activation_ok, activation_reason = validate_activation(db, publication, activation, now=now)
        validated = validate_authorization(db, publication, authorization, now=now, dispatch_provider="buffer")
        ok, reason = validate_pilot(db, publication, settings)
        readiness = evaluate_buffer_pilot_execution_readiness(
            db, publication.id, evidence=execution_evidence, settings=settings, now=now,
        )
    if readiness["execution_status"] != FINAL_EXECUTION_READY:
        raise ManualDispatchError("BUFFER_EXECUTION_GATE_LOCKED")
    if not activation_ok:
        raise ManualDispatchError(activation_reason)
    if not validated["valid"]:
        raise ManualDispatchError(validated["status"])
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
    attempt = atomic_authorized_claim(db, publication, authorization, now=now, dispatch_provider="buffer",
                                      buffer_activation=activation)
    if attempt is None:
        raise ManualDispatchError("AUTHORIZED_CLAIM_FAILED")
    publication_id, attempt_id, actor = publication.id, attempt.id, authorization.authorized_by
    try:
        db.refresh(publication)
        db.refresh(authorization)
        db.refresh(attempt)
        if not validate_post_claim(db, publication, authorization, attempt, now=now, dispatch_provider="buffer")["valid"]:
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
        if not validate_post_claim(db, publication, authorization, attempt, now=now, dispatch_provider="buffer")["valid"]:
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
    mutation_diagnostic = None
    try:
        result = await gateway.create_pinterest_post(payload)
    except BufferDefinitiveRejection as error:
        status, code = PublicationStatus.PUBLISH_FAILED, "PROVIDER_REJECTED"
        mutation_diagnostic = error.safe_diagnostic()
    except BufferAmbiguousFailure as error:
        status, code = PublicationStatus.PUBLISH_UNKNOWN, "PUBLISH_UNKNOWN"
        mutation_diagnostic = error.safe_diagnostic()
    except Exception:
        # Any unexpected exception at the mutation boundary is uncertain, sanitized, and not retryable.
        status, code = PublicationStatus.PUBLISH_UNKNOWN, "PUBLISH_UNKNOWN"
        mutation_diagnostic = _unexpected_diagnostic()
    else:
        status = PublicationStatus.PUBLISH_FAILED if result.status == "error" else PublicationStatus.PUBLISH_UNKNOWN
        code = "BUFFER_PROVIDER_FAILED" if result.status == "error" else "BUFFER_FINAL_OUTCOME_PENDING"
    _log_mutation_outcome(publication_id, attempt_id, diagnostic=mutation_diagnostic, result=result)
    persisted = _persist(db, publication_id, attempt_id, status, code, record_settings,
                         result=result, mutation_diagnostic=mutation_diagnostic)
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
