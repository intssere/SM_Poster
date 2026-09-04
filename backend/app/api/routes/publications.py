from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models.domain import PinPublication, PinApproval, PinterestBoard, PinterestConnection, PublicationStatus, PublicationAttempt
from app.services.publication_identity import PublicationIdentityService, PublicationIdentityError
from app.services.publication_scheduler import schedule, cancel, due_publications
from app.services.pinterest_publisher import publication_readiness, sanitize_metadata, PublicationReconciliationError
from app.services.pinterest_publisher import preflight_publish_readiness, execution_publish_readiness, finalize_post_claim_unknown
from app.services.manual_publication_dispatch import ManualDispatchError
from app.services import manual_publication_dispatch

router = APIRouter(prefix="/publications", tags=["publications"])

class PublicationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approval_id: str
    pinterest_connection_id: str
    pinterest_board_id: str
    scheduled_for: datetime | None = None

class ScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scheduled_for: datetime

def _dto(db, row):
    from app.core.config import get_settings
    live = get_settings().publishing_enabled
    attempts = [{"attempt_number": a.attempt_number, "status": a.status, "started_at": a.started_at, "completed_at": a.completed_at, "provider_pin_id": a.provider_pin_id, "error_code": a.error_code, "safe_response_metadata": sanitize_metadata(a.safe_response_metadata)} for a in getattr(row, "_attempts", [])]
    return {"id": row.id, "status": row.status.value if hasattr(row.status, "value") else row.status, "revision_id": row.revision_id, "creative_id": row.creative_id, "approval_id": row.approval_id, "pinterest_connection_id": row.pinterest_connection_id, "pinterest_board_record_id": row.pinterest_board_record_id, "pinterest_board_id": row.pinterest_board_id_snapshot or row.pinterest_board_id, "title": row.title_snapshot, "description": row.description_snapshot, "alt_text": row.alt_text_snapshot, "destination_url": row.destination_url, "utm_url": row.utm_url, "media_url": row.media_url_snapshot, "scheduled_for": row.scheduled_for, "published_at": row.published_at, "pinterest_pin_id": row.pinterest_pin_id, "error_code": row.error_code, "scheduler_foundation_available": True, "live_publishing_enabled": live, "publishing_readiness_reason": publication_readiness(db, row), "attempts": attempts}

@router.post("")
def create(payload: PublicationCreate, db: Session = Depends(get_db)):
    board = db.get(PinterestBoard, payload.pinterest_board_id)
    connection = db.get(PinterestConnection, payload.pinterest_connection_id)
    if not board or not connection or board.connection_id != connection.id:
        raise HTTPException(422, "Invalid Pinterest destination")
    approval = db.get(PinApproval, payload.approval_id)
    if not approval:
        raise HTTPException(404, "Approval not found")
    try:
        row = PublicationIdentityService(lambda: db).create_snapshot(approval_id=payload.approval_id, board_id="", pinterest_connection_id=connection.id, pinterest_board_record_id=board.id, scheduled_for=payload.scheduled_for)
    except PublicationIdentityError as exc:
        raise HTTPException(422, str(exc))
    return _dto(db, row)

@router.get("")
def list_publications(db: Session = Depends(get_db)):
    return [_dto(db, row) for row in db.scalars(select(PinPublication).order_by(PinPublication.created_at.desc())).all()]

@router.get("/{publication_id}")
def get_publication(publication_id: str, db: Session = Depends(get_db)):
    row = db.get(PinPublication, publication_id)
    if not row: raise HTTPException(404, "Publication not found")
    row._attempts = db.scalars(select(PublicationAttempt).where(PublicationAttempt.publication_id == row.id).order_by(PublicationAttempt.attempt_number)).all()
    return _dto(db, row)

@router.post("/{publication_id}/schedule")
def reschedule(publication_id: str, payload: ScheduleRequest, db: Session = Depends(get_db)):
    if payload.scheduled_for.tzinfo is None: raise HTTPException(422, "scheduled_for must include timezone")
    row = db.get(PinPublication, publication_id)
    if not row: raise HTTPException(404, "Publication not found")
    try: return _dto(db, schedule(db, row, payload.scheduled_for.astimezone(timezone.utc)))
    except ValueError as exc: raise HTTPException(409, str(exc))

@router.post("/{publication_id}/cancel")
def cancel_publication(publication_id: str, db: Session = Depends(get_db)):
    row = db.get(PinPublication, publication_id)
    if not row: raise HTTPException(404, "Publication not found")
    try: return _dto(db, cancel(db, row))
    except ValueError as exc: raise HTTPException(409, str(exc))

@router.post("/{publication_id}/publish")
async def publish(publication_id: str, db: Session = Depends(get_db)):
    row = db.get(PinPublication, publication_id)
    if not row: raise HTTPException(404, "Publication not found")
    try:
        await manual_publication_dispatch.dispatch_publication(db, row)
    except ManualDispatchError as exc:
        detail = str(exc)
        if detail == "TOKEN_DECRYPT_FAILED":
            raise HTTPException(502, detail) from None
        raise HTTPException(409, detail) from None
    except HTTPException: raise
    except PublicationReconciliationError: raise
    except Exception:
        raise HTTPException(502, "Pinterest publication failed") from None
    return _dto(db, row)
