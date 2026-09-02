from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models.domain import PinPublication, PinApproval, PinterestBoard, PinterestConnection, PublicationStatus
from app.services.publication_identity import PublicationIdentityService, PublicationIdentityError
from app.services.publication_scheduler import schedule, cancel, due_publications, claim
from app.services.pinterest_publisher import publishing_ready

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

def _dto(row):
    return {"id": row.id, "status": row.status.value if hasattr(row.status, "value") else row.status, "revision_id": row.revision_id, "creative_id": row.creative_id, "approval_id": row.approval_id, "pinterest_connection_id": row.pinterest_connection_id, "pinterest_board_record_id": row.pinterest_board_record_id, "pinterest_board_id": row.pinterest_board_id_snapshot or row.pinterest_board_id, "title": row.title_snapshot, "description": row.description_snapshot, "alt_text": row.alt_text_snapshot, "destination_url": row.destination_url, "utm_url": row.utm_url, "media_url": row.media_url_snapshot, "scheduled_for": row.scheduled_for, "published_at": row.published_at, "pinterest_pin_id": row.pinterest_pin_id, "error_code": row.error_code}

@router.post("")
def create(payload: PublicationCreate, db: Session = Depends(get_db)):
    board = db.get(PinterestBoard, payload.pinterest_board_id)
    connection = db.get(PinterestConnection, payload.pinterest_connection_id)
    if not board or not connection or board.connection_id != connection.id:
        raise HTTPException(422, "Invalid Pinterest destination")
    approval = db.get(PinApproval, payload.approval_id)
    if not approval:
        raise HTTPException(404, "Approval not found")
    # Legacy board FK is retained for historical rows; resolve only an existing store board.
    from app.models.domain import Board
    legacy = db.scalar(select(Board).where(Board.pinterest_board_id == board.external_board_id))
    if not legacy:
        raise HTTPException(422, "Publication destination is not mapped")
    try:
        row = PublicationIdentityService(lambda: db).create_snapshot(approval_id=payload.approval_id, board_id=legacy.id, pinterest_connection_id=connection.id, pinterest_board_record_id=board.id, scheduled_for=payload.scheduled_for)
    except PublicationIdentityError as exc:
        raise HTTPException(422, str(exc))
    return _dto(row)

@router.get("")
def list_publications(db: Session = Depends(get_db)):
    return [_dto(row) for row in db.scalars(select(PinPublication).order_by(PinPublication.created_at.desc())).all()]

@router.get("/{publication_id}")
def get_publication(publication_id: str, db: Session = Depends(get_db)):
    row = db.get(PinPublication, publication_id)
    if not row: raise HTTPException(404, "Publication not found")
    return _dto(row)

@router.post("/{publication_id}/schedule")
def reschedule(publication_id: str, payload: ScheduleRequest, db: Session = Depends(get_db)):
    if payload.scheduled_for.tzinfo is None: raise HTTPException(422, "scheduled_for must include timezone")
    row = db.get(PinPublication, publication_id)
    if not row: raise HTTPException(404, "Publication not found")
    try: return _dto(schedule(db, row, payload.scheduled_for.astimezone(timezone.utc)))
    except ValueError as exc: raise HTTPException(409, str(exc))

@router.post("/{publication_id}/cancel")
def cancel_publication(publication_id: str, db: Session = Depends(get_db)):
    row = db.get(PinPublication, publication_id)
    if not row: raise HTTPException(404, "Publication not found")
    try: return _dto(cancel(db, row))
    except ValueError as exc: raise HTTPException(409, str(exc))

@router.post("/{publication_id}/publish")
async def publish(publication_id: str, db: Session = Depends(get_db)):
    raise HTTPException(409, "Publishing is disabled")
