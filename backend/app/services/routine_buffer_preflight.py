from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.core.config import Settings, get_settings
from app.integrations.buffer.gateway import BufferGateway
from app.models.domain import PinPublication
from app.services.buffer_execution_preflight import BufferPreflightError, verify_live_image
from app.services.buffer_pinterest_adapter import verify_destination
from app.services.publication_scheduler import request_fingerprint_for
from app.services.pinterest_publisher import normalize_persisted_utc
from app.services.routine_dispatch_authorization import active_permit, validate_permit


@dataclass(frozen=True)
class RoutineExecutionEvidence:
    publication_id: str
    publication_fingerprint: str
    request_fingerprint: str
    buffer_organization_id: str
    buffer_pinterest_channel_id: str
    board_service_id: str
    media_url: str
    observed_at: datetime
    provider_destination_live_verified: bool
    media_live_fetch_verified: bool


def evidence_matches(evidence: RoutineExecutionEvidence, publication: PinPublication, settings: Settings) -> bool:
    return all((
        evidence.publication_id == publication.id,
        evidence.publication_fingerprint == publication.publication_fingerprint,
        evidence.request_fingerprint == request_fingerprint_for(publication),
        evidence.buffer_organization_id == settings.buffer_organization_id,
        evidence.buffer_pinterest_channel_id == settings.buffer_pinterest_channel_id,
        evidence.board_service_id == publication.pinterest_board_id_snapshot,
        evidence.media_url == publication.media_url_snapshot,
        evidence.provider_destination_live_verified is True,
        evidence.media_live_fetch_verified is True,
    ))


async def build_routine_execution_evidence(
    db,
    publication: PinPublication,
    *,
    settings: Settings | None = None,
    gateway: BufferGateway | None = None,
    media_client=None,
    resolver=None,
    now: datetime | None = None,
) -> RoutineExecutionEvidence:
    settings = settings or get_settings()
    now = normalize_persisted_utc(now or datetime.now(timezone.utc))
    permit = active_permit(db, publication.id)
    permitted = validate_permit(db, publication, permit, now=now, require_due=True)
    if not permitted["valid"]:
        raise BufferPreflightError(permitted["status"])
    if not isinstance(settings.buffer_api_key, str) or not settings.buffer_api_key.strip():
        raise BufferPreflightError("BUFFER_CONFIGURATION_REQUIRED")
    if not settings.buffer_organization_id or not settings.buffer_pinterest_channel_id:
        raise BufferPreflightError("BUFFER_CONFIGURATION_REQUIRED")
    gateway = gateway or BufferGateway(settings)
    try:
        destination = await verify_destination(publication, settings, gateway)
        await verify_live_image(publication.media_url_snapshot, client=media_client, resolver=resolver)
    except BufferPreflightError:
        raise
    except Exception:
        raise BufferPreflightError("ROUTINE_BUFFER_PREFLIGHT_FAILED") from None
    return RoutineExecutionEvidence(
        publication_id=publication.id,
        publication_fingerprint=publication.publication_fingerprint,
        request_fingerprint=request_fingerprint_for(publication),
        buffer_organization_id=destination["organization_id"],
        buffer_pinterest_channel_id=destination["channel_id"],
        board_service_id=destination["board_service_id"],
        media_url=publication.media_url_snapshot,
        observed_at=now,
        provider_destination_live_verified=True,
        media_live_fetch_verified=True,
    )
