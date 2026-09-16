from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.models.domain import PinPublication, PublicationStatus
from app.models.routine_publishing import RoutineDispatchPermit
from app.services.pinterest_publication_quality import PINTEREST_QUALITY_V1, validate_publication_quality
from app.services.publication_dispatch_authorization import manual_structural_readiness
from app.services.publication_duplicates import SAFE_TO_CONTINUE, evaluate_publication_duplicates
from app.services.publication_scheduler import request_fingerprint_for
from app.services.pinterest_publisher import normalize_persisted_utc

PERMIT_TTL = timedelta(hours=24)


class RoutinePermitError(RuntimeError):
    pass


def _now():
    return datetime.now(timezone.utc)


def active_permit(db, publication_id: str):
    return db.scalar(select(RoutineDispatchPermit).where(
        RoutineDispatchPermit.publication_id == publication_id,
        RoutineDispatchPermit.status == "ACTIVE",
    ).order_by(RoutineDispatchPermit.authorized_at.desc()).limit(1))


def expire_stale_permits(db, publication_id: str, *, now=None):
    now = normalize_persisted_utc(now or _now())
    result = db.execute(update(RoutineDispatchPermit).where(
        RoutineDispatchPermit.publication_id == publication_id,
        RoutineDispatchPermit.status == "ACTIVE",
        RoutineDispatchPermit.expires_at <= now,
    ).values(status="EXPIRED"))
    return int(result.rowcount or 0)


def _snapshots(db, publication, *, now, expected_status=PublicationStatus.SCHEDULED, require_due=False):
    quality = validate_publication_quality(db, publication, dispatch_provider="buffer")
    duplicate = evaluate_publication_duplicates(db, publication)
    readiness = manual_structural_readiness(
        db,
        publication,
        now=now,
        expected_publication_state=expected_status,
        require_due=require_due,
        dispatch_provider="buffer",
    )
    return quality, duplicate, readiness


def create_permit(db, publication: PinPublication, *, actor: str, now=None):
    now = normalize_persisted_utc(now or _now())
    if not actor:
        raise RoutinePermitError("ACTOR_REQUIRED")
    if publication.status != PublicationStatus.SCHEDULED or not publication.scheduled_for:
        raise RoutinePermitError("PUBLICATION_NOT_SCHEDULED")
    expire_stale_permits(db, publication.id, now=now)
    db.flush()
    if active_permit(db, publication.id):
        raise RoutinePermitError("ACTIVE_ROUTINE_PERMIT_EXISTS")
    quality, duplicate, readiness = _snapshots(db, publication, now=now)
    if quality["status"] != "PASS":
        raise RoutinePermitError("QUALITY_WARNING" if quality["status"] == "WARNING" else "QUALITY_FAILED")
    if duplicate["status"] != SAFE_TO_CONTINUE:
        raise RoutinePermitError(duplicate["status"])
    if not readiness["ready"]:
        raise RoutinePermitError(readiness["status"])
    scheduled_for = normalize_persisted_utc(publication.scheduled_for)
    expires_at = max(now + PERMIT_TTL, scheduled_for + timedelta(hours=2))
    permit = RoutineDispatchPermit(
        publication_id=publication.id,
        dispatch_provider="buffer",
        approval_id=publication.approval_id,
        pinterest_board_record_id=publication.pinterest_board_record_id,
        publication_fingerprint=publication.publication_fingerprint,
        request_fingerprint=request_fingerprint_for(publication),
        scheduled_for_snapshot=scheduled_for,
        quality_policy_version=PINTEREST_QUALITY_V1,
        quality_snapshot=deepcopy(quality),
        duplicate_snapshot=deepcopy(duplicate),
        readiness_snapshot=deepcopy(readiness),
        authorized_by=actor[:255],
        authorized_at=now,
        expires_at=expires_at,
        status="ACTIVE",
    )
    db.add(permit)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise RoutinePermitError("ACTIVE_ROUTINE_PERMIT_EXISTS") from None
    db.refresh(permit)
    return permit


def revoke_permit(db, permit, *, actor: str, reason: str, now=None):
    if not actor:
        raise RoutinePermitError("ACTOR_REQUIRED")
    if permit.status != "ACTIVE":
        raise RoutinePermitError("ROUTINE_PERMIT_NOT_ACTIVE")
    if not reason or len(reason) > 255:
        raise RoutinePermitError("INVALID_REVOKE_REASON")
    now = normalize_persisted_utc(now or _now())
    result = db.execute(update(RoutineDispatchPermit).where(
        RoutineDispatchPermit.id == permit.id,
        RoutineDispatchPermit.status == "ACTIVE",
    ).values(status="REVOKED", revoked_at=now, revoked_by=actor[:255], revoke_reason=reason))
    if result.rowcount != 1:
        db.rollback()
        raise RoutinePermitError("ROUTINE_PERMIT_NOT_ACTIVE")
    db.commit()
    db.refresh(permit)
    return permit


def validate_permit(db, publication, permit, *, now=None, expected_status=PublicationStatus.SCHEDULED, require_due=True, allowed_status="ACTIVE"):
    now = normalize_persisted_utc(now or _now())
    if not permit or permit.status != allowed_status:
        return {"valid": False, "status": "ROUTINE_PERMIT_REQUIRED" if permit is None else f"ROUTINE_PERMIT_{permit.status}"}
    if permit.publication_id != publication.id or permit.dispatch_provider != "buffer":
        return {"valid": False, "status": "ROUTINE_PERMIT_MISMATCH"}
    if normalize_persisted_utc(permit.expires_at) <= now:
        return {"valid": False, "status": "ROUTINE_PERMIT_EXPIRED"}
    if permit.publication_fingerprint != publication.publication_fingerprint:
        return {"valid": False, "status": "ROUTINE_PERMIT_MISMATCH"}
    if permit.approval_id != publication.approval_id or permit.pinterest_board_record_id != publication.pinterest_board_record_id:
        return {"valid": False, "status": "ROUTINE_PERMIT_MISMATCH"}
    if permit.request_fingerprint != request_fingerprint_for(publication):
        return {"valid": False, "status": "ROUTINE_PERMIT_MISMATCH"}
    if normalize_persisted_utc(permit.scheduled_for_snapshot) != normalize_persisted_utc(publication.scheduled_for):
        return {"valid": False, "status": "ROUTINE_PERMIT_SCHEDULE_DRIFT"}
    quality, duplicate, readiness = _snapshots(
        db, publication, now=now, expected_status=expected_status, require_due=require_due
    )
    if permit.quality_policy_version != PINTEREST_QUALITY_V1:
        return {"valid": False, "status": "ROUTINE_PERMIT_POLICY_DRIFT"}
    if quality != permit.quality_snapshot or duplicate != permit.duplicate_snapshot:
        return {"valid": False, "status": "ROUTINE_PERMIT_SNAPSHOT_DRIFT"}
    if readiness.get("ready") is not True:
        return {"valid": False, "status": readiness.get("status", "ROUTINE_READINESS_FAILED")}
    # Creation may have been before due time, so compare the structural identity fields
    # rather than requiring an identical transient due-time result.
    for key in ("dispatch_provider",):
        if permit.readiness_snapshot.get(key) != readiness.get(key):
            return {"valid": False, "status": "ROUTINE_PERMIT_SNAPSHOT_DRIFT"}
    return {"valid": True, "status": "ACTIVE", "quality": quality, "duplicate": duplicate, "readiness": readiness}
