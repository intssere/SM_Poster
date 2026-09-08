"""Explicit future operator action; no runtime invocation or provider I/O."""
from dataclasses import dataclass
from datetime import datetime, timezone
import re

from sqlalchemy import update
from app.core.config import get_settings
from app.models.domain import Board, AuditLog
from app.services.publication_dispatch_authorization import AUTHORIZATION_TTL
from app.services.pinterest_publisher import normalize_persisted_utc


@dataclass(frozen=True)
class BufferDestinationBindingEvidence:
    buffer_organization_id: str
    buffer_pinterest_channel_id: str
    board_service_id: str
    observed_at: datetime
    provider_destination_live_verified: bool = False


def bind_buffer_destination(db, board_id, *, actor, evidence=None, settings=None, now=None):
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 255:
        raise RuntimeError("ACTOR_REQUIRED")
    if not isinstance(evidence, BufferDestinationBindingEvidence) or evidence.provider_destination_live_verified is not True:
        raise RuntimeError("BUFFER_DESTINATION_EVIDENCE_REQUIRED")
    now = normalize_persisted_utc(now or datetime.now(timezone.utc))
    if not isinstance(evidence.observed_at, datetime):
        raise RuntimeError("BUFFER_DESTINATION_EVIDENCE_STALE")
    observed = normalize_persisted_utc(evidence.observed_at)
    if observed > now or now - observed > AUTHORIZATION_TTL:
        raise RuntimeError("BUFFER_DESTINATION_EVIDENCE_STALE")
    settings = settings or get_settings()
    values = (evidence.buffer_organization_id, evidence.buffer_pinterest_channel_id, evidence.board_service_id)
    if (any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", v) for v in values)
            or evidence.buffer_organization_id != settings.buffer_organization_id
            or evidence.buffer_pinterest_channel_id != settings.buffer_pinterest_channel_id):
        raise RuntimeError("BUFFER_DESTINATION_EVIDENCE_MISMATCH")
    with db.no_autoflush:
        board = db.get(Board, board_id)
        if not board: raise RuntimeError("BUFFER_BOARD_NOT_FOUND")
        if not board.active: raise RuntimeError("BUFFER_BOARD_INACTIVE")
        if board.pinterest_board_id == evidence.board_service_id: return board
        if board.pinterest_board_id: raise RuntimeError("BUFFER_BOARD_BINDING_CONFLICT")
        try:
            result = db.execute(update(Board).where(Board.id == board_id, Board.active.is_(True),
                               Board.pinterest_board_id.is_(None)).values(pinterest_board_id=evidence.board_service_id))
            if result.rowcount != 1: raise RuntimeError("BUFFER_BOARD_BINDING_CONFLICT")
            db.add(AuditLog(actor=actor, action="BUFFER_BOARD_BOUND", entity_type="Board", entity_id=board_id,
                           metadata_json={"board_id": board_id, "board_service_id": evidence.board_service_id}))
            db.commit()
        except Exception:
            db.rollback()
            raise RuntimeError("BUFFER_BOARD_BINDING_FAILED") from None
        db.refresh(board)
        return board
