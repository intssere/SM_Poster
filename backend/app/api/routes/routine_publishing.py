from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.auth import current_user
from app.core.config import get_settings
from app.db.session import get_db
from app.models.domain import PinPublication
from app.models.routine_publishing import RoutineDispatchPermit
from app.services.routine_dispatch_authorization import RoutinePermitError, active_permit, create_permit, revoke_permit
from app.services.routine_publishing_control import RoutineControlError, routine_operational_snapshot, set_control

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
