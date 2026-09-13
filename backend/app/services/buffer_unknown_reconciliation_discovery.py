"""Read-only discovery for Buffer PUBLISH_UNKNOWN attempts without operation IDs.

This service never mutates provider or database state and never authorizes retry.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re

from sqlalchemy import select

from app.core.config import get_settings
from app.integrations.buffer.gateway import (
    BufferGateway,
    BufferConfigurationError,
    BufferReadError,
)
from app.models.domain import (
    Board,
    PinConcept,
    PinDraft,
    PinPublication,
    PinterestBoard,
    PinterestConnection,
    PublicationAttempt,
    PublicationStatus,
)
from app.services.buffer_publication_reconciliation import pinterest_pin_id, snapshot_matches
from app.services.publication_scheduler import request_fingerprint_for


class BufferUnknownDiscoveryError(RuntimeError):
    """Bounded discovery failure. Never includes provider body or credentials."""


DISCOVERY_STATUSES = ("draft", "error", "needs_approval", "scheduled", "sending", "sent")
WINDOW_MARGIN = timedelta(minutes=15)
LIST_PAGE_SIZE = 100
MAX_EXACT_READS = 24
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,255}")


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _provider_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return _utc(parsed)


def _attempt_window(attempt: PublicationAttempt) -> tuple[datetime, datetime]:
    points = [
        _utc(attempt.created_at),
        _utc(attempt.started_at),
        _utc(attempt.completed_at),
        _utc(attempt.provider_submitted_at),
        _utc(attempt.provider_last_observed_at),
    ]
    points = [point for point in points if point is not None]
    if not points:
        raise BufferUnknownDiscoveryError("BUFFER_DISCOVERY_ATTEMPT_TIME_REQUIRED")
    return min(points) - WINDOW_MARGIN, max(points) + WINDOW_MARGIN


def _listed_post_in_window(post, start: datetime, end: datetime) -> bool:
    timestamps = [
        _provider_time(post.created_at),
        _provider_time(post.due_at),
        _provider_time(post.sent_at),
    ]
    known = [value for value in timestamps if value is not None]
    # Missing provider timestamps must not be used to prove absence. Include the
    # row conservatively and let the bounded exact-read cap fail closed if needed.
    return not known or any(start <= value <= end for value in known)


def _server_owned_routing_valid(db, publication: PinPublication) -> bool:
    if not (publication.pinterest_board_record_id and publication.pinterest_connection_id):
        return False
    board = db.get(PinterestBoard, publication.pinterest_board_record_id)
    connection = db.get(PinterestConnection, publication.pinterest_connection_id)
    return bool(
        board
        and connection
        and connection.status == "CONNECTED"
        and board.connection_id == connection.id
        and board.is_active
        and board.is_eligible
        and board.last_synced_at is not None
        and connection.boards_last_synced_at is not None
        and board.last_synced_at == connection.boards_last_synced_at
        and publication.pinterest_board_id_snapshot
        and publication.pinterest_board_id_snapshot == board.external_board_id
    )


def _legacy_routing_valid(db, publication: PinPublication) -> bool:
    if publication.pinterest_board_record_id or publication.pinterest_connection_id:
        return False
    board = db.get(Board, publication.board_id) if publication.board_id else None
    draft = db.get(PinDraft, publication.draft_id) if publication.draft_id else None
    concept = db.get(PinConcept, draft.concept_id) if draft else None
    return bool(
        board
        and board.active
        and board.pinterest_board_id
        and publication.pinterest_board_id_snapshot
        and board.pinterest_board_id == publication.pinterest_board_id_snapshot
        and concept
        and concept.board_id == board.id
        and concept.store_id == board.store_id
    )


def _entry(db, publication_id: str, settings):
    publication = db.get(PinPublication, publication_id, populate_existing=True)
    if not publication or publication.status != PublicationStatus.PUBLISH_UNKNOWN:
        raise BufferUnknownDiscoveryError("DISCOVERY_REQUIRES_PUBLISH_UNKNOWN")

    attempts = db.scalars(
        select(PublicationAttempt)
        .where(PublicationAttempt.publication_id == publication_id)
        .order_by(PublicationAttempt.attempt_number)
        .execution_options(populate_existing=True)
    ).all()
    if len(attempts) != 1:
        raise BufferUnknownDiscoveryError("BUFFER_DISCOVERY_SINGLE_ATTEMPT_REQUIRED")
    attempt = attempts[0]
    if (
        attempt.dispatch_provider != "buffer"
        or attempt.status != "UNKNOWN"
        or attempt.provider_operation_id is not None
    ):
        raise BufferUnknownDiscoveryError("BUFFER_DISCOVERY_NO_OPERATION_UNKNOWN_REQUIRED")
    if publication.pinterest_pin_id or attempt.provider_pin_id:
        raise BufferUnknownDiscoveryError("BUFFER_DISCOVERY_KNOWN_PIN_PRESENT")
    if not publication.publication_fingerprint or attempt.request_fingerprint != request_fingerprint_for(publication):
        raise BufferUnknownDiscoveryError("BUFFER_DISCOVERY_SNAPSHOT_MISMATCH")

    org_id = getattr(settings, "buffer_organization_id", None)
    channel_id = getattr(settings, "buffer_pinterest_channel_id", None)
    if (
        not isinstance(org_id, str)
        or not isinstance(channel_id, str)
        or not _SAFE_ID.fullmatch(org_id)
        or not _SAFE_ID.fullmatch(channel_id)
    ):
        raise BufferUnknownDiscoveryError("BUFFER_DISCOVERY_CONFIGURATION_REQUIRED")

    metadata = attempt.safe_response_metadata or {}
    observed_org = metadata.get("buffer_organization_id")
    observed_channel = metadata.get("buffer_channel_id")
    if observed_org is not None and observed_org != org_id:
        raise BufferUnknownDiscoveryError("BUFFER_DISCOVERY_PROVIDER_IDENTITY_MISMATCH")
    if observed_channel is not None and observed_channel != channel_id:
        raise BufferUnknownDiscoveryError("BUFFER_DISCOVERY_PROVIDER_IDENTITY_MISMATCH")

    has_new_routing = bool(publication.pinterest_board_record_id or publication.pinterest_connection_id)
    routing_valid = _server_owned_routing_valid(db, publication) if has_new_routing else _legacy_routing_valid(db, publication)
    if not routing_valid:
        raise BufferUnknownDiscoveryError("BUFFER_DISCOVERY_ROUTING_MISMATCH")

    return publication, attempt


def _safe_match(snapshot) -> dict:
    pin = pinterest_pin_id(snapshot.external_link) if snapshot.status == "sent" else None
    return {
        "provider_operation_id": snapshot.buffer_post_id,
        "provider_operation_status": snapshot.status,
        "provider_pin_id": pin,
        "provider_pin_verified": bool(pin),
    }


async def discover_buffer_unknown(db, publication_id: str, *, settings=None, gateway=None) -> dict:
    """Discover exact Buffer candidates without writing DB/provider state.

    Absence is never proof of failure and every successful classification keeps
    retry permission false.
    """
    settings = settings or get_settings()
    publication, attempt = _entry(db, publication_id, settings)
    start, end = _attempt_window(attempt)
    gateway = gateway or BufferGateway(settings)

    listed = {}
    try:
        for status in DISCOVERY_STATUSES:
            rows = await gateway.recent_posts(
                settings.buffer_organization_id,
                settings.buffer_pinterest_channel_id,
                first=LIST_PAGE_SIZE,
                status=status,
            )
            for row in rows:
                if _listed_post_in_window(row, start, end):
                    listed[row.buffer_post_id] = row
    except (BufferReadError, BufferConfigurationError):
        raise BufferUnknownDiscoveryError("BUFFER_DISCOVERY_READ_FAILED") from None

    if len(listed) > MAX_EXACT_READS:
        raise BufferUnknownDiscoveryError("BUFFER_DISCOVERY_CANDIDATE_LIMIT_EXCEEDED")

    matches = []
    try:
        for operation_id in sorted(listed):
            snapshot = await gateway.post(operation_id)
            if snapshot_matches(publication, snapshot, settings):
                matches.append(_safe_match(snapshot))
    except (BufferReadError, BufferConfigurationError):
        raise BufferUnknownDiscoveryError("BUFFER_DISCOVERY_READ_FAILED") from None

    if not matches:
        classification = "ZERO_MATCH"
    elif len(matches) == 1:
        classification = "UNIQUE_MATCH"
    else:
        classification = "MULTIPLE_MATCHES"

    return {
        "classification": classification,
        "publication_id": publication.id,
        "attempt_id": attempt.id,
        "provider": "buffer",
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "listed_candidate_count": len(listed),
        "exact_read_count": len(listed),
        "match_count": len(matches),
        "matches": matches,
        "state_mutated": False,
        "absence_proves_failure": False,
        "retry_permitted": False,
    }
