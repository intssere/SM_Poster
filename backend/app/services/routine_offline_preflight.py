from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.models.domain import PinPublication, PinterestBoard, PinterestConnection
from app.services.publication_scheduler import request_fingerprint_for
from app.services.pinterest_publisher import normalize_persisted_utc
from app.services.routine_dispatch_authorization import validate_permit


PERSISTED_ROUTE_MAX_AGE = timedelta(hours=24)


class RoutineOfflinePreflightError(RuntimeError):
    pass


@dataclass(frozen=True)
class RoutineOfflineEvidence:
    publication_id: str
    publication_fingerprint: str
    request_fingerprint: str
    pinterest_connection_id: str
    pinterest_board_record_id: str
    board_service_id: str
    observed_at: datetime
    permit_validated: bool
    quality_passed: bool
    duplicate_safe: bool
    persisted_route_validated: bool
    external_requests: int = 0


def _persisted_route(db, publication: PinPublication, *, now: datetime):
    if not publication.pinterest_connection_id or not publication.pinterest_board_record_id:
        raise RoutineOfflinePreflightError("PERSISTED_PINTEREST_ROUTING_REQUIRED")
    connection = db.get(PinterestConnection, publication.pinterest_connection_id)
    board = db.get(PinterestBoard, publication.pinterest_board_record_id)
    if not (
        connection
        and board
        and connection.provider == "pinterest"
        and connection.status == "CONNECTED"
        and board.connection_id == connection.id
        and board.is_active
        and board.is_eligible
        and board.last_synced_at is not None
        and connection.boards_last_synced_at is not None
        and normalize_persisted_utc(board.last_synced_at)
        == normalize_persisted_utc(connection.boards_last_synced_at)
        and publication.pinterest_board_id_snapshot
        and board.external_board_id == publication.pinterest_board_id_snapshot
    ):
        raise RoutineOfflinePreflightError("PERSISTED_PINTEREST_ROUTING_STALE")
    synced_at = normalize_persisted_utc(board.last_synced_at)
    if now - synced_at > PERSISTED_ROUTE_MAX_AGE:
        raise RoutineOfflinePreflightError("PERSISTED_PINTEREST_ROUTING_TOO_OLD")
    return connection, board


def build_routine_offline_evidence(
    db,
    publication: PinPublication,
    *,
    permit,
    now: datetime | None = None,
) -> RoutineOfflineEvidence:
    """Build provider-free DRY_RUN evidence from persisted state only.

    This module intentionally imports no provider gateway, HTTP/DNS client, media
    verifier, or dispatch service. LIVE provider preflight remains separate.
    """
    now = normalize_persisted_utc(now or datetime.now(timezone.utc))
    validated = validate_permit(
        db, publication, permit, now=now, require_due=True,
    )
    if validated.get("valid") is not True:
        raise RoutineOfflinePreflightError(
            str(validated.get("status") or "ROUTINE_PERMIT_INVALID")
        )
    quality = validated.get("quality") or {}
    duplicate = validated.get("duplicate") or {}
    if quality.get("status") != "PASS":
        raise RoutineOfflinePreflightError("QUALITY_NOT_PASS")
    if duplicate.get("status") != "SAFE_TO_CONTINUE":
        raise RoutineOfflinePreflightError(
            str(duplicate.get("status") or "DUPLICATE_CHECK_FAILED")
        )
    connection, board = _persisted_route(db, publication, now=now)
    return RoutineOfflineEvidence(
        publication_id=publication.id,
        publication_fingerprint=publication.publication_fingerprint,
        request_fingerprint=request_fingerprint_for(publication),
        pinterest_connection_id=connection.id,
        pinterest_board_record_id=board.id,
        board_service_id=board.external_board_id,
        observed_at=now,
        permit_validated=True,
        quality_passed=True,
        duplicate_safe=True,
        persisted_route_validated=True,
        external_requests=0,
    )
