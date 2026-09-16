from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from app.core.config import Settings, get_settings
from app.integrations.buffer.gateway import (
    BufferAmbiguousFailure,
    BufferConfigurationError,
    BufferDefinitiveRejection,
    BufferGateway,
    BufferReadError,
)
from app.models.domain import PinPublication, PublicationAttempt, PublicationStatus
from app.models.routine_publishing import RoutineAttemptBoundary, RoutineDispatchPermit
from app.services.buffer_pinterest_adapter import build_pinterest_payload, verify_destination
from app.services.buffer_publication_reconciliation import BufferReconciliationError, reconcile_buffer
from app.services.publication_scheduler import request_fingerprint_for
from app.services.pinterest_publisher import PublicationReconciliationError, normalize_persisted_utc
from app.services.routine_buffer_preflight import RoutineExecutionEvidence, evidence_matches
from app.services.routine_dispatch_authorization import active_permit, validate_permit
from app.services.routine_publishing_control import get_control, pause_on_unknown


class RoutineDispatchError(RuntimeError):
    pass


def _now():
    return datetime.now(timezone.utc)


def _live_gates(settings: Settings, control) -> tuple[bool, str]:
    for enabled, code in (
        (settings.publishing_enabled, "PUBLISHING_DISABLED"),
        (settings.buffer_publishing_enabled, "BUFFER_PUBLISHING_DISABLED"),
        (settings.routine_pinterest_worker_enabled, "ROUTINE_WORKER_DISABLED"),
        (settings.routine_buffer_dispatch_enabled, "ROUTINE_BUFFER_DISPATCH_DISABLED"),
    ):
        if enabled is not True:
            return False, code
    if settings.routine_pinterest_dry_run is True:
        return False, "ROUTINE_DRY_RUN_ENABLED"
    if not control or control.state != "LIVE":
        return False, "ROUTINE_CONTROL_NOT_LIVE"
    return True, "READY"


def claim_for_routine(db, publication, permit, *, now=None):
    now = normalize_persisted_utc(now or _now())
    validation = validate_permit(db, publication, permit, now=now, require_due=True)
    if not validation["valid"]:
        raise RoutineDispatchError(validation["status"])
    try:
        pub = db.execute(update(PinPublication).execution_options(synchronize_session=False).where(
            PinPublication.id == publication.id,
            PinPublication.status == PublicationStatus.SCHEDULED,
            PinPublication.scheduled_for.is_not(None),
            PinPublication.scheduled_for <= now,
        ).values(status=PublicationStatus.PUBLISHING, attempt_started_at=now))
        if pub.rowcount != 1:
            db.rollback()
            return None
        consumed = db.execute(update(RoutineDispatchPermit).execution_options(synchronize_session=False).where(
            RoutineDispatchPermit.id == permit.id,
            RoutineDispatchPermit.publication_id == publication.id,
            RoutineDispatchPermit.status == "ACTIVE",
            RoutineDispatchPermit.dispatch_provider == "buffer",
            RoutineDispatchPermit.publication_fingerprint == publication.publication_fingerprint,
            RoutineDispatchPermit.request_fingerprint == request_fingerprint_for(publication),
            RoutineDispatchPermit.expires_at > now,
        ).values(status="CONSUMED", consumed_at=now))
        if consumed.rowcount != 1:
            db.rollback()
            return None
        attempt_no = (db.scalar(select(PublicationAttempt.attempt_number).where(
            PublicationAttempt.publication_id == publication.id
        ).order_by(PublicationAttempt.attempt_number.desc()).limit(1)) or 0) + 1
        claimed = db.get(PinPublication, publication.id)
        attempt = PublicationAttempt(
            publication_id=publication.id,
            attempt_number=attempt_no,
            status="STARTED",
            dispatch_provider="buffer",
            request_fingerprint=request_fingerprint_for(claimed or publication),
            safe_response_metadata={},
        )
        db.add(attempt)
        db.flush()
        boundary = RoutineAttemptBoundary(
            attempt_id=attempt.id,
            publication_id=publication.id,
            routine_dispatch_permit_id=permit.id,
            claimed_at=now,
            safe_metadata={},
        )
        db.add(boundary)
        db.commit()
        db.refresh(attempt)
        return attempt
    except Exception:
        db.rollback()
        raise


def _persist_pre_provider_failure(db, publication_id, attempt_id, code):
    now = _now()
    changed = db.execute(update(PinPublication).where(
        PinPublication.id == publication_id,
        PinPublication.status == PublicationStatus.PUBLISHING,
    ).values(status=PublicationStatus.PUBLISH_FAILED, error_code=code))
    changed_attempt = db.execute(update(PublicationAttempt).where(
        PublicationAttempt.id == attempt_id,
        PublicationAttempt.publication_id == publication_id,
        PublicationAttempt.status == "STARTED",
    ).values(status="FAILED", error_code=code, completed_at=now))
    if changed.rowcount != 1 or changed_attempt.rowcount != 1:
        db.rollback()
        raise PublicationReconciliationError("ROUTINE_PRE_PROVIDER_PERSISTENCE_CONFLICT")
    db.commit()


def mark_provider_mutation_boundary(db, attempt_id: str, *, now=None):
    now = normalize_persisted_utc(now or _now())
    changed = db.execute(update(RoutineAttemptBoundary).where(
        RoutineAttemptBoundary.attempt_id == attempt_id,
        RoutineAttemptBoundary.provider_mutation_started_at.is_(None),
    ).values(provider_mutation_started_at=now))
    if changed.rowcount != 1:
        db.rollback()
        raise RoutineDispatchError("ROUTINE_PROVIDER_BOUNDARY_CONFLICT")
    db.commit()
    return now


def _unexpected_diagnostic():
    return {
        "outcome_class": "ambiguous",
        "failure_code": "unexpected_exception",
        "phase": "mutation_boundary",
        "response_received": None,
        "http_status": None,
        "request_send_state": "unknown",
    }


def _persist_provider_outcome(db, publication_id, attempt_id, *, status, code, settings, result=None, diagnostic=None):
    now = _now()
    metadata = {
        "buffer_channel_id": settings.buffer_pinterest_channel_id,
        "buffer_organization_id": settings.buffer_organization_id,
    }
    if diagnostic is not None:
        metadata["mutation"] = {key: diagnostic.get(key) for key in (
            "outcome_class", "failure_code", "phase", "response_received", "http_status", "request_send_state"
        )}
        metadata["mutation"]["observed_at"] = now.isoformat()
    terminal_failed = status == PublicationStatus.PUBLISH_FAILED
    attempt_values = {
        "status": "FAILED" if terminal_failed else "UNKNOWN",
        "error_code": code,
        "completed_at": now if terminal_failed else None,
        "safe_response_metadata": metadata,
    }
    if result is not None:
        attempt_values.update({
            "provider_operation_id": result.buffer_post_id,
            "provider_operation_status": result.status,
            "provider_external_link": result.external_link,
            "provider_submitted_at": now,
            "provider_last_observed_at": now,
        })
    try:
        changed = db.execute(update(PinPublication).where(
            PinPublication.id == publication_id,
            PinPublication.status == PublicationStatus.PUBLISHING,
        ).values(status=status, error_code=code))
        changed_attempt = db.execute(update(PublicationAttempt).where(
            PublicationAttempt.id == attempt_id,
            PublicationAttempt.publication_id == publication_id,
            PublicationAttempt.status == "STARTED",
            PublicationAttempt.dispatch_provider == "buffer",
        ).values(**attempt_values))
        if changed.rowcount != 1 or changed_attempt.rowcount != 1:
            raise PublicationReconciliationError("ROUTINE_BUFFER_RESULT_STATE_CONFLICT")
        db.commit()
    except Exception:
        db.rollback()
        raise PublicationReconciliationError("ROUTINE_BUFFER_RESULT_PERSISTENCE_FAILED") from None


def recover_stale_routine_claims(db, *, stale_seconds: int, now=None):
    now = normalize_persisted_utc(now or _now())
    cutoff = now - timedelta(seconds=stale_seconds)
    rows = db.execute(select(PinPublication, PublicationAttempt, RoutineAttemptBoundary).join(
        PublicationAttempt, PublicationAttempt.publication_id == PinPublication.id
    ).join(
        RoutineAttemptBoundary, RoutineAttemptBoundary.attempt_id == PublicationAttempt.id
    ).where(
        PinPublication.status == PublicationStatus.PUBLISHING,
        PublicationAttempt.status == "STARTED",
        PublicationAttempt.started_at <= cutoff,
    )).all()
    recovered = []
    for publication, attempt, boundary in rows:
        if boundary.provider_mutation_started_at is not None:
            publication.status = PublicationStatus.PUBLISH_UNKNOWN
            publication.error_code = "ROUTINE_STALE_AFTER_PROVIDER_BOUNDARY"
            attempt.status = "UNKNOWN"
            attempt.error_code = "ROUTINE_STALE_AFTER_PROVIDER_BOUNDARY"
            pause_on_unknown(db, publication.id, reason="ROUTINE_STALE_AFTER_PROVIDER_BOUNDARY")
            recovered.append((publication.id, "PUBLISH_UNKNOWN"))
        else:
            publication.status = PublicationStatus.PUBLISH_FAILED
            publication.error_code = "ROUTINE_STALE_BEFORE_PROVIDER_BOUNDARY"
            attempt.status = "FAILED"
            attempt.error_code = "ROUTINE_STALE_BEFORE_PROVIDER_BOUNDARY"
            attempt.completed_at = now
            db.commit()
            recovered.append((publication.id, "PUBLISH_FAILED"))
    return recovered


async def dispatch_routine_buffer(
    db,
    publication,
    *,
    evidence: RoutineExecutionEvidence,
    settings: Settings | None = None,
    gateway: BufferGateway | None = None,
    now=None,
):
    settings = settings or get_settings()
    now = normalize_persisted_utc(now or _now())
    permit = active_permit(db, publication.id)
    control = get_control(db)
    ready, reason = _live_gates(settings, control)
    if not ready:
        raise RoutineDispatchError(reason)
    if not permit or not evidence_matches(evidence, publication, settings):
        raise RoutineDispatchError("ROUTINE_EXECUTION_EVIDENCE_MISMATCH")
    if normalize_persisted_utc(evidence.observed_at) > now or now - normalize_persisted_utc(evidence.observed_at) > timedelta(minutes=15):
        raise RoutineDispatchError("ROUTINE_EXECUTION_EVIDENCE_STALE")
    gateway = gateway or BufferGateway(settings)
    attempt = claim_for_routine(db, publication, permit, now=now)
    if attempt is None:
        raise RoutineDispatchError("ROUTINE_AUTHORIZED_CLAIM_FAILED")
    publication_id, attempt_id = publication.id, attempt.id
    try:
        db.refresh(publication)
        db.refresh(permit)
        post = validate_permit(
            db, publication, permit, now=now, expected_status=PublicationStatus.PUBLISHING,
            require_due=True, allowed_status="CONSUMED",
        )
        if not post["valid"]:
            raise RoutineDispatchError(post["status"])
        control = get_control(db)
        ready, reason = _live_gates(settings, control)
        if not ready:
            raise RoutineDispatchError(reason)
        await verify_destination(publication, settings, gateway)
        db.refresh(publication)
        db.refresh(permit)
        post = validate_permit(
            db, publication, permit, now=now, expected_status=PublicationStatus.PUBLISHING,
            require_due=True, allowed_status="CONSUMED",
        )
        if not post["valid"]:
            raise RoutineDispatchError(post["status"])
        control = get_control(db)
        ready, reason = _live_gates(settings, control)
        if not ready:
            raise RoutineDispatchError(reason)
        payload = build_pinterest_payload(publication, settings)
    except Exception:
        db.rollback()
        _persist_pre_provider_failure(db, publication_id, attempt_id, "ROUTINE_PRE_PROVIDER_VALIDATION_FAILED")
        raise RoutineDispatchError("ROUTINE_PRE_PROVIDER_VALIDATION_FAILED") from None

    mark_provider_mutation_boundary(db, attempt_id, now=now)
    result = None
    diagnostic = None
    try:
        result = await gateway.create_pinterest_post(payload)
    except BufferDefinitiveRejection as exc:
        status, code, diagnostic = PublicationStatus.PUBLISH_FAILED, "PROVIDER_REJECTED", exc.safe_diagnostic()
    except BufferAmbiguousFailure as exc:
        status, code, diagnostic = PublicationStatus.PUBLISH_UNKNOWN, "PUBLISH_UNKNOWN", exc.safe_diagnostic()
    except Exception:
        status, code, diagnostic = PublicationStatus.PUBLISH_UNKNOWN, "PUBLISH_UNKNOWN", _unexpected_diagnostic()
    else:
        status = PublicationStatus.PUBLISH_FAILED if result.status == "error" else PublicationStatus.PUBLISH_UNKNOWN
        code = "BUFFER_PROVIDER_FAILED" if result.status == "error" else "BUFFER_FINAL_OUTCOME_PENDING"
    _persist_provider_outcome(
        db, publication_id, attempt_id, status=status, code=code, settings=settings,
        result=result, diagnostic=diagnostic,
    )
    if status == PublicationStatus.PUBLISH_UNKNOWN:
        pause_on_unknown(db, publication_id)
    if result is not None and result.status == "sent":
        try:
            await reconcile_buffer(db, publication_id, actor="routine-worker", settings=settings, gateway=gateway)
        except BufferReconciliationError:
            try:
                db.execute(update(PinPublication).where(
                    PinPublication.id == publication_id,
                    PinPublication.status == PublicationStatus.PUBLISH_UNKNOWN,
                ).values(error_code="BUFFER_SENT_LINK_UNVERIFIED"))
                db.execute(update(PublicationAttempt).where(
                    PublicationAttempt.id == attempt_id,
                    PublicationAttempt.status == "UNKNOWN",
                ).values(error_code="BUFFER_SENT_LINK_UNVERIFIED"))
                db.commit()
            except Exception:
                db.rollback()
                raise PublicationReconciliationError("ROUTINE_RECONCILIATION_PERSISTENCE_FAILED") from None
    db.refresh(publication)
    return publication
