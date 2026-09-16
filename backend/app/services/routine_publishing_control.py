from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models.domain import PinPublication, PublicationAttempt, PublicationStatus
from app.models.routine_publishing import RoutineAttemptBoundary, RoutinePublishingControl, RoutinePublishingRun

CONTROL_ID = "default"


class RoutineControlError(RuntimeError):
    pass


def utcnow():
    return datetime.now(timezone.utc)


def get_control(db, *, create=True):
    row = db.get(RoutinePublishingControl, CONTROL_ID)
    if row or not create:
        return row
    row = RoutinePublishingControl(id=CONTROL_ID, state="PAUSED", pause_reason="ROUTINE_CONTROL_NOT_ARMED", paused_at=utcnow(), paused_by="system")
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        row = db.get(RoutinePublishingControl, CONTROL_ID)
    return row


def set_control(db, *, state: str, actor: str, reason: str | None = None):
    if state not in {"PAUSED", "DRY_RUN", "LIVE"}:
        raise RoutineControlError("INVALID_ROUTINE_CONTROL_STATE")
    if not actor:
        raise RoutineControlError("ACTOR_REQUIRED")
    row = get_control(db)
    row.state = state
    if state == "PAUSED":
        row.pause_reason = (reason or "OPERATOR_PAUSE")[:255]
        row.paused_at = utcnow()
        row.paused_by = actor[:255]
    else:
        row.pause_reason = None
        row.paused_at = None
        row.paused_by = None
    db.commit()
    db.refresh(row)
    return row


def pause_on_unknown(db, publication_id: str, *, reason="PUBLISH_UNKNOWN_CIRCUIT_BREAKER"):
    row = get_control(db)
    row.state = "PAUSED"
    row.pause_reason = reason[:255]
    row.paused_at = utcnow()
    row.paused_by = "routine-worker"
    row.last_unknown_publication_id = publication_id
    db.commit()
    return row


def start_run(db, *, mode: str, now=None):
    now = now or utcnow()
    row = RoutinePublishingRun(mode=mode, started_at=now, heartbeat_at=now, status="RUNNING")
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise RoutineControlError("ROUTINE_WORKER_ALREADY_RUNNING") from None
    db.refresh(row)
    return row


def finish_run(db, run, *, status="SUCCEEDED", error_code=None, now=None):
    run.status = status
    run.error_code = error_code
    run.heartbeat_at = now or utcnow()
    run.completed_at = now or utcnow()
    db.commit()
    return run


def daily_provider_write_count(db, *, day_start):
    return int(db.scalar(
        select(func.count(RoutineAttemptBoundary.id)).where(
            RoutineAttemptBoundary.provider_mutation_started_at.is_not(None),
            RoutineAttemptBoundary.provider_mutation_started_at >= day_start,
        )
    ) or 0)


def routine_operational_snapshot(db):
    control = get_control(db, create=False)
    latest = db.scalar(select(RoutinePublishingRun).order_by(RoutinePublishingRun.started_at.desc()).limit(1))
    due = int(db.scalar(select(func.count(PinPublication.id)).where(
        PinPublication.status == PublicationStatus.SCHEDULED,
        PinPublication.scheduled_for <= utcnow(),
    )) or 0)
    publishing = int(db.scalar(select(func.count(PinPublication.id)).where(PinPublication.status == PublicationStatus.PUBLISHING)) or 0)
    unknown = int(db.scalar(select(func.count(PinPublication.id)).where(PinPublication.status == PublicationStatus.PUBLISH_UNKNOWN)) or 0)
    latest_boundary = db.scalar(select(RoutineAttemptBoundary).where(
        RoutineAttemptBoundary.provider_mutation_started_at.is_not(None)
    ).order_by(RoutineAttemptBoundary.provider_mutation_started_at.desc()).limit(1))
    latest_attempt = db.get(PublicationAttempt, latest_boundary.attempt_id) if latest_boundary else None
    return {
        "control_state": control.state if control else "PAUSED",
        "pause_reason": control.pause_reason if control else "ROUTINE_CONTROL_NOT_INITIALIZED",
        "last_unknown_publication_id": control.last_unknown_publication_id if control else None,
        "latest_run": ({
            "id": latest.id,
            "mode": latest.mode,
            "status": latest.status,
            "started_at": latest.started_at,
            "completed_at": latest.completed_at,
            "scanned": latest.scanned,
            "eligible": latest.eligible,
            "claimed": latest.claimed,
            "dispatched": latest.dispatched,
            "published": latest.published,
            "failed": latest.failed,
            "unknown": latest.unknown,
            "error_code": latest.error_code,
        } if latest else None),
        "due_backlog": due,
        "publishing_count": publishing,
        "publish_unknown_count": unknown,
        "last_provider_mutation_started_at": latest_boundary.provider_mutation_started_at if latest_boundary else None,
        "last_provider_operation_status": latest_attempt.provider_operation_status if latest_attempt else None,
    }
