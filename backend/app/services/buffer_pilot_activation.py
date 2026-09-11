"""Server-owned lifecycle for the Buffer single-pin pilot."""
from datetime import datetime, timezone
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from app.models.domain import (
    BufferPilotActivation, PinApproval, PinPublication, PinterestBoard,
    PinterestConnection,
    PublicationAttempt, PublicationStatus,
)
from app.services.publication_scheduler import request_fingerprint_for


class BufferPilotActivationError(RuntimeError):
    pass


def active_activation(db, publication_id=None):
    query = select(BufferPilotActivation).where(BufferPilotActivation.status == "ACTIVE")
    if publication_id:
        query = query.where(BufferPilotActivation.publication_id == publication_id)
    return db.scalar(query.limit(1))


def activate(db, *, publication_id, actor, now=None):
    now = now or datetime.now(timezone.utc)
    if not actor:
        raise BufferPilotActivationError("ACTOR_REQUIRED")
    if active_activation(db):
        raise BufferPilotActivationError("ACTIVE_ACTIVATION_EXISTS")
    publication = db.get(PinPublication, publication_id)
    approval = db.get(PinApproval, publication.approval_id) if publication else None
    pinterest_board_record_id = publication.pinterest_board_record_id if publication else None
    board = db.get(PinterestBoard, pinterest_board_record_id)
    connection = db.get(PinterestConnection, board.connection_id) if board else None
    if (not approval or approval.decision != "APPROVED" or not board
            or not connection or connection.status != "CONNECTED"
            or board.is_active is not True or board.is_eligible is not True
            or not board.last_synced_at
            or not connection.boards_last_synced_at
            or board.last_synced_at != connection.boards_last_synced_at):
        raise BufferPilotActivationError("DESTINATION_NOT_ELIGIBLE")
    if not publication or publication.pinterest_board_record_id != board.id:
        raise BufferPilotActivationError("PUBLICATION_NOT_READY")
    if publication.status != PublicationStatus.SCHEDULED or not publication.scheduled_for or not publication.pinterest_connection_id:
        raise BufferPilotActivationError("PUBLICATION_NOT_READY")
    if publication.pinterest_board_record_id != board.id or publication.pinterest_board_id_snapshot != board.external_board_id:
        raise BufferPilotActivationError("DESTINATION_DRIFT")
    if db.scalar(select(PublicationAttempt).where(PublicationAttempt.publication_id == publication.id)):
        raise BufferPilotActivationError("PRIOR_ATTEMPT_EXISTS")
    activation = BufferPilotActivation(
        approval_id=approval.id, publication_id=publication.id, pinterest_board_record_id=board.id,
        publication_fingerprint=publication.publication_fingerprint,
        request_fingerprint=request_fingerprint_for(publication), actor=actor,
        activated_at=now, status="ACTIVE",
    )
    db.add(activation)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise BufferPilotActivationError("ACTIVE_ACTIVATION_EXISTS") from None
    db.refresh(activation)
    return activation


def revoke(db, activation, *, actor, reason, now=None):
    if not actor:
        raise BufferPilotActivationError("ACTOR_REQUIRED")
    if activation.status != "ACTIVE":
        raise BufferPilotActivationError("ACTIVATION_NOT_ACTIVE")
    revoked_at = now or datetime.now(timezone.utc)
    result = db.execute(update(BufferPilotActivation).where(
        BufferPilotActivation.id == activation.id,
        BufferPilotActivation.status == "ACTIVE",
    ).values(status="REVOKED", revoked_at=revoked_at, revoked_by=actor,
             revoke_reason=reason))
    if result.rowcount != 1:
        db.rollback()
        raise BufferPilotActivationError("ACTIVATION_NOT_ACTIVE")
    db.commit()
    db.refresh(activation)
    return activation


def validate_activation(db, publication, activation, *, now=None):
    now = now or datetime.now(timezone.utc)
    if not activation or activation.status != "ACTIVE":
        return False, "ACTIVATION_REQUIRED"
    if activation.expires_at and activation.expires_at <= now:
        return False, "ACTIVATION_EXPIRED"
    if activation.publication_fingerprint != publication.publication_fingerprint:
        return False, "ACTIVATION_MISMATCH"
    if activation.request_fingerprint != request_fingerprint_for(publication):
        return False, "ACTIVATION_MISMATCH"
    if (activation.publication_id != publication.id or activation.approval_id != publication.approval_id
            or activation.pinterest_board_record_id != publication.pinterest_board_record_id):
        return False, "ACTIVATION_MISMATCH"
    board = db.get(PinterestBoard, publication.pinterest_board_record_id)
    connection = db.get(PinterestConnection, publication.pinterest_connection_id)
    if (not board or not connection or connection.status != "CONNECTED"
            or board.connection_id != connection.id or not board.is_active
            or not board.is_eligible or not board.last_synced_at
            or not connection.boards_last_synced_at
            or board.last_synced_at != connection.boards_last_synced_at
            or board.external_board_id != publication.pinterest_board_id_snapshot):
        return False, "DESTINATION_DRIFT"
    if db.scalar(select(PublicationAttempt).where(PublicationAttempt.publication_id == publication.id)):
        return False, "PRIOR_ATTEMPT_EXISTS"
    return True, "ACTIVE"


def consume(db, activation, publication, *, now=None):
    now = now or datetime.now(timezone.utc)
    result = db.execute(update(BufferPilotActivation).where(
        BufferPilotActivation.id == activation.id, BufferPilotActivation.status == "ACTIVE",
        BufferPilotActivation.publication_id == publication.id,
        BufferPilotActivation.approval_id == publication.approval_id,
        BufferPilotActivation.pinterest_board_record_id == publication.pinterest_board_record_id,
        BufferPilotActivation.publication_fingerprint == publication.publication_fingerprint,
        BufferPilotActivation.request_fingerprint == request_fingerprint_for(publication),
    ).values(status="CONSUMED", consumed_at=now))
    if result.rowcount != 1:
        return False
    return True


# Explicit service name used by control-plane callers.
activate_buffer_pilot = activate