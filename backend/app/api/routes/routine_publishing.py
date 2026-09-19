from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import current_user
from app.core.config import get_settings
from app.db.session import get_db
from app.models.domain import PinPublication
from app.models.routine_publishing import RoutineDispatchPermit, RoutinePublishingRun
from app.services.publication_scheduler import request_fingerprint_for
from app.services.routine_dispatch_authorization import (
    RoutinePermitError,
    active_permit,
    create_permit,
    revoke_permit,
    validate_permit,
)
from app.services.routine_pinterest_worker import run_once as run_routine_worker_once
from app.services.routine_publishing_control import (
    RoutineControlError,
    daily_provider_write_count,
    get_control,
    routine_operational_snapshot,
    set_control,
)

router = APIRouter(prefix="/routine-publishing", tags=["routine-publishing"])


class PermitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: bool
    confirmation_text_version: str


class PermitRevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=255)


class ControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: str
    confirmed: bool
    confirmation_text_version: str
    reason: str | None = Field(default=None, max_length=255)


class RunOnceDryRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: bool
    confirmation_text_version: str


class RunOnceLiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: bool
    confirmation_text_version: str
    permit_id: str = Field(min_length=1, max_length=64)
    publication_fingerprint: str = Field(min_length=64, max_length=64)
    request_fingerprint: str = Field(min_length=64, max_length=64)


def _one_shot_dry_run_settings(settings, control):
    if not control or control.state != "DRY_RUN":
        raise RoutineControlError("ROUTINE_CONTROL_NOT_DRY_RUN")
    if settings.routine_pinterest_worker_enabled is not False:
        raise RoutineControlError("ROUTINE_WORKER_MUST_BE_DISABLED")
    if settings.routine_buffer_dispatch_enabled is not False:
        raise RoutineControlError("ROUTINE_BUFFER_DISPATCH_MUST_BE_DISABLED")
    if settings.routine_pinterest_dry_run is not True:
        raise RoutineControlError("ROUTINE_DRY_RUN_CONFIG_REQUIRED")
    if settings.routine_pinterest_batch_size != 1:
        raise RoutineControlError("ROUTINE_DRY_RUN_BATCH_SIZE_MUST_BE_ONE")
    if settings.routine_pinterest_daily_write_limit != 1:
        raise RoutineControlError("ROUTINE_DRY_RUN_DAILY_LIMIT_MUST_BE_ONE")
    return settings.model_copy(update={
        "routine_pinterest_worker_enabled": True,
        "routine_buffer_dispatch_enabled": False,
        "routine_pinterest_dry_run": True,
    })


def _one_shot_live_settings(settings, control):
    if not control or control.state != "PAUSED":
        raise RoutineControlError("ROUTINE_CONTROL_NOT_PAUSED")
    if settings.routine_pinterest_worker_enabled is not False:
        raise RoutineControlError("ROUTINE_WORKER_MUST_BE_DISABLED")
    if settings.routine_buffer_dispatch_enabled is not False:
        raise RoutineControlError("ROUTINE_BUFFER_DISPATCH_MUST_BE_DISABLED")
    if settings.routine_pinterest_dry_run is not True:
        raise RoutineControlError("ROUTINE_DRY_RUN_CONFIG_REQUIRED")
    if settings.routine_pinterest_batch_size != 1:
        raise RoutineControlError("ROUTINE_LIVE_BATCH_SIZE_MUST_BE_ONE")
    if settings.routine_pinterest_daily_write_limit != 1:
        raise RoutineControlError("ROUTINE_LIVE_DAILY_LIMIT_MUST_BE_ONE")
    if settings.publishing_enabled is not True:
        raise RoutineControlError("PUBLISHING_DISABLED")
    if settings.buffer_publishing_enabled is not True:
        raise RoutineControlError("BUFFER_PUBLISHING_DISABLED")
    return settings.model_copy(update={
        "routine_pinterest_worker_enabled": True,
        "routine_buffer_dispatch_enabled": True,
        "routine_pinterest_dry_run": False,
    })


def _active_running_run(db: Session):
    return db.scalar(select(RoutinePublishingRun).where(
        RoutinePublishingRun.status == "RUNNING"
    ).limit(1))


def _actor(request: Request):
    actor = current_user(request)
    if not actor:
        raise HTTPException(401, "Authentication required")
    return actor


@router.get("/status")
def status(db: Session = Depends(get_db)):
    settings = get_settings()
    snapshot = routine_operational_snapshot(db)
    return {
        "worker_enabled": settings.routine_pinterest_worker_enabled,
        "buffer_dispatch_enabled": settings.routine_buffer_dispatch_enabled,
        "dry_run": settings.routine_pinterest_dry_run,
        "batch_size": settings.routine_pinterest_batch_size,
        "daily_write_limit": settings.routine_pinterest_daily_write_limit,
        **snapshot,
    }


@router.post("/publications/{publication_id}/run-once-dry-run")
async def run_once_dry_run(
    publication_id: str,
    request: Request,
    payload: RunOnceDryRunRequest,
    db: Session = Depends(get_db),
):
    _actor(request)
    if not payload.confirmed or payload.confirmation_text_version != "ROUTINE_DRY_RUN_ONCE_V1":
        raise HTTPException(422, "INVALID_ROUTINE_DRY_RUN_CONFIRMATION")
    settings = get_settings()
    control = get_control(db, create=False)
    try:
        effective_settings = _one_shot_dry_run_settings(settings, control)
    except RoutineControlError as exc:
        raise HTTPException(409, str(exc)) from None
    now = datetime.now(timezone.utc)
    result = await run_routine_worker_once(
        db,
        settings=effective_settings,
        now=now,
        target_publication_id=publication_id,
    )
    if result.get("status") != "SUCCEEDED" or result.get("mode") != "DRY_RUN":
        raise HTTPException(409, "ROUTINE_DRY_RUN_DID_NOT_COMPLETE")
    if any(int(result.get(key, 0) or 0) != 0 for key in ("claimed", "dispatched", "published", "failed", "unknown")):
        raise HTTPException(500, "ROUTINE_DRY_RUN_MUTATION_INVARIANT_FAILED")
    if (
        int(result.get("scanned", 0) or 0) != 1
        or int(result.get("eligible", 0) or 0) != 1
        or int(result.get("skipped", 0) or 0) != 0
    ):
        raise HTTPException(409, "ROUTINE_DRY_RUN_TARGET_NOT_ELIGIBLE")
    return result


@router.post("/publications/{publication_id}/run-once-live")
async def run_once_live(
    publication_id: str,
    request: Request,
    payload: RunOnceLiveRequest,
    db: Session = Depends(get_db),
):
    actor = _actor(request)
    if not payload.confirmed or payload.confirmation_text_version != "ROUTINE_LIVE_ONCE_V1":
        raise HTTPException(422, "INVALID_ROUTINE_LIVE_CONFIRMATION")

    settings = get_settings()
    control = get_control(db, create=False)
    try:
        effective_settings = _one_shot_live_settings(settings, control)
    except RoutineControlError as exc:
        raise HTTPException(409, str(exc)) from None

    publication = db.get(PinPublication, publication_id)
    if not publication:
        raise HTTPException(404, "Publication not found")
    current_request_fingerprint = request_fingerprint_for(publication)
    if publication.publication_fingerprint != payload.publication_fingerprint:
        raise HTTPException(409, "ROUTINE_PUBLICATION_FINGERPRINT_MISMATCH")
    if current_request_fingerprint != payload.request_fingerprint:
        raise HTTPException(409, "ROUTINE_REQUEST_FINGERPRINT_MISMATCH")

    now = datetime.now(timezone.utc)
    permit = active_permit(db, publication_id)
    if not permit or permit.id != payload.permit_id:
        raise HTTPException(409, "ROUTINE_PERMIT_ID_MISMATCH")
    validated = validate_permit(db, publication, permit, now=now, require_due=True)
    if not validated["valid"]:
        raise HTTPException(409, validated["status"])

    if _active_running_run(db) is not None:
        raise HTTPException(409, "ROUTINE_WORKER_ALREADY_RUNNING")
    day_start = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    if daily_provider_write_count(db, day_start=day_start) >= settings.routine_pinterest_daily_write_limit:
        raise HTTPException(409, "ROUTINE_DAILY_WRITE_LIMIT_REACHED")

    live_armed = False
    result = None
    pause_reason = "ONE_SHOT_LIVE_COMPLETE"
    try:
        set_control(db, state="LIVE", actor=actor, reason="ONE_SHOT_LIVE_AUTHORIZED")
        live_armed = True
        result = await run_routine_worker_once(
            db,
            settings=effective_settings,
            now=now,
            target_publication_id=publication_id,
            target_permit_id=payload.permit_id,
            allow_targeted_live=True,
        )
        if int(result.get("unknown", 0) or 0) > 0:
            pause_reason = "PUBLISH_UNKNOWN_CIRCUIT_BREAKER"
        if result.get("status") != "SUCCEEDED" or result.get("mode") != "LIVE":
            raise HTTPException(409, result.get("status") or "ROUTINE_LIVE_DID_NOT_COMPLETE")
        return result
    except Exception:
        if result is None:
            pause_reason = "ONE_SHOT_LIVE_EXCEPTION"
        raise
    finally:
        if live_armed:
            db.rollback()
            set_control(db, state="PAUSED", actor=actor, reason=pause_reason)


@router.get("/publications/{publication_id}/permit")
def permit_status(publication_id: str, db: Session = Depends(get_db)):
    permit = active_permit(db, publication_id)
    if not permit:
        return {"status": "DISARMED"}
    return {
        "id": permit.id,
        "status": permit.status,
        "dispatch_provider": permit.dispatch_provider,
        "authorized_at": permit.authorized_at,
        "expires_at": permit.expires_at,
        "scheduled_for_snapshot": permit.scheduled_for_snapshot,
    }


@router.post("/publications/{publication_id}/permit")
def arm_permit(publication_id: str, request: Request, payload: PermitRequest, db: Session = Depends(get_db)):
    actor = _actor(request)
    if not payload.confirmed or payload.confirmation_text_version != "ROUTINE_BUFFER_DISPATCH_V1":
        raise HTTPException(422, "INVALID_ROUTINE_PERMIT_CONFIRMATION")
    publication = db.get(PinPublication, publication_id)
    if not publication:
        raise HTTPException(404, "Publication not found")
    try:
        permit = create_permit(db, publication, actor=actor)
    except RoutinePermitError as exc:
        raise HTTPException(409, str(exc)) from None
    return {
        "id": permit.id,
        "status": permit.status,
        "dispatch_provider": permit.dispatch_provider,
        "authorized_at": permit.authorized_at,
        "expires_at": permit.expires_at,
    }


@router.post("/publications/{publication_id}/permit/revoke")
def revoke(publication_id: str, request: Request, payload: PermitRevokeRequest, db: Session = Depends(get_db)):
    actor = _actor(request)
    permit = active_permit(db, publication_id)
    if not permit:
        raise HTTPException(404, "Active routine permit not found")
    try:
        revoke_permit(db, permit, actor=actor, reason=payload.reason)
    except RoutinePermitError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"id": permit.id, "status": permit.status, "revoked_at": permit.revoked_at}


@router.post("/control")
def control(request: Request, payload: ControlRequest, db: Session = Depends(get_db)):
    actor = _actor(request)
    if not payload.confirmed or payload.confirmation_text_version != "ROUTINE_PUBLISHING_CONTROL_V1":
        raise HTTPException(422, "INVALID_ROUTINE_CONTROL_CONFIRMATION")
    try:
        row = set_control(db, state=payload.state, actor=actor, reason=payload.reason)
    except RoutineControlError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"state": row.state, "pause_reason": row.pause_reason, "paused_at": row.paused_at}
