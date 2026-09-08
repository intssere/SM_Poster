"""Read-only Buffer live-pilot static certification.

This module produces a bounded dossier for a single explicit publication ID.
It performs no provider HTTP, no gateway construction, and no database writes.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.integrations.buffer.gateway import BufferConfigurationError
from app.models.domain import (
    PinPublication,
    ProductImage,
    PublicationAttempt,
    PublicationReconciliationEvent,
    PublicationStatus,
)
from app.services.buffer_pinterest_adapter import build_pinterest_payload
from app.services.publication_dispatch_authorization import (
    latest_authorization,
    manual_structural_readiness,
)
from app.services.publication_duplicates import SAFE_TO_CONTINUE, evaluate_publication_duplicates
from app.services.pinterest_publication_quality import validate_publication_quality
from app.services.pinterest_publisher import media_publishable, normalize_persisted_utc
from app.services.publication_scheduler import request_fingerprint_for


ALLOWED_BUFFER_API_BASES = {"https://api.buffer.com", "https://api.buffer.com/"}
BUFFER_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9_-]{1,255}\Z")


def _status(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _iso(value: datetime | None) -> str | None:
    normalized = normalize_persisted_utc(value)
    return normalized.isoformat() if normalized else None


def _credential_configured(settings: Settings) -> bool:
    return isinstance(settings.buffer_api_key, str) and bool(settings.buffer_api_key.strip())


def _buffer_identifier_valid(value: str | None) -> bool:
    return isinstance(value, str) and bool(BUFFER_IDENTIFIER_RE.fullmatch(value))


def _latest_authorization_status(db: Session, publication_id: str, now: datetime) -> dict[str, Any]:
    authorization = latest_authorization(db, publication_id)
    if authorization is None:
        return {"status": "NOT_CREATED", "authorization_id": None}
    expires_at = normalize_persisted_utc(authorization.expires_at)
    status = authorization.status
    if status == "ACTIVE" and expires_at and expires_at <= now:
        status = "EXPIRED"
    return {
        "status": status,
        "authorization_id": authorization.id,
        "expires_at": _iso(authorization.expires_at),
        "consumed_at": _iso(authorization.consumed_at),
        "revoked_at": _iso(authorization.revoked_at),
    }


def _buffer_dossier(settings: Settings) -> dict[str, Any]:
    return {
        "organization_configured": _buffer_identifier_valid(settings.buffer_organization_id),
        "channel_configured": _buffer_identifier_valid(settings.buffer_pinterest_channel_id),
        "api_base_valid": settings.buffer_api_base in ALLOWED_BUFFER_API_BASES,
        "credential_configured": _credential_configured(settings),
        "write_credential_authorized": False,
        "provider_destination_live_verified": False,
        "external_link_format_live_certified": False,
    }


def _protected_gates(settings: Settings) -> dict[str, bool]:
    return {
        "publishing_enabled": bool(settings.publishing_enabled),
        "buffer_publishing_enabled": bool(settings.buffer_publishing_enabled),
        "buffer_single_pin_pilot_enabled": bool(settings.buffer_single_pin_pilot_enabled),
    }


def _base_dossier(
    *,
    publication_id: str | None,
    settings: Settings,
    now: datetime,
    publication: PinPublication | None = None,
    request_fingerprint: str | None = None,
    authorization: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": "STATIC_CANDIDATE_BLOCKED",
        "live_execution": "LOCKED",
        "publication_id": publication_id,
        "publication_fingerprint": publication.publication_fingerprint if publication else None,
        "request_fingerprint": request_fingerprint,
        "certified_at": now.isoformat(),
        "identity": {
            "revision_id": publication.revision_id if publication else None,
            "creative_id": publication.creative_id if publication else None,
            "source_image_id": publication.source_image_id if publication else None,
            "approval_id": publication.approval_id if publication else None,
        },
        "pinterest_destination": {
            "connection_id": publication.pinterest_connection_id if publication else None,
            "board_record_id": publication.pinterest_board_record_id if publication else None,
            "board_service_id": publication.pinterest_board_id_snapshot if publication else None,
            "local_binding_valid": False,
            "provider_verified_live": False,
        },
        "payload": {
            "title": publication.title_snapshot if publication else None,
            "description": publication.description_snapshot if publication else None,
            "alt_text": publication.alt_text_snapshot if publication else None,
            "destination_url": publication.destination_url if publication else None,
            "utm_url": publication.utm_url if publication else None,
            "media_url": publication.media_url_snapshot if publication else None,
        },
        "media": {
            "structurally_valid": bool(publication and media_publishable(publication.media_url_snapshot)),
            "provenance_present": False,
            "live_fetch_verified": False,
        },
        "quality": {"status": None, "policy_version": None},
        "duplicate": {"status": None},
        "attempts": {"count": 0, "known_pinterest_pin": bool(publication and publication.pinterest_pin_id)},
        "reconciliation": {"count": 0},
        "authorization": authorization or {"status": "NOT_CREATED", "authorization_id": None},
        "buffer": _buffer_dossier(settings),
        "protected_gates": _protected_gates(settings),
    }


def _blocked(dossier: dict[str, Any], status: str) -> dict[str, Any]:
    result = deepcopy(dossier)
    result["status"] = status
    result["live_execution"] = "LOCKED"
    return result


def certify_buffer_pilot_candidate(
    db: Session,
    publication_id: str,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a safe static certification dossier for one explicit publication."""
    settings = settings or get_settings()
    now = normalize_persisted_utc(now or datetime.now(timezone.utc))
    with db.no_autoflush:
        if not isinstance(publication_id, str) or not publication_id.strip():
            return _blocked(
                _base_dossier(publication_id=None, settings=settings, now=now),
                "PUBLICATION_ID_REQUIRED",
            )

        publication = db.get(PinPublication, publication_id)
        if publication is None:
            return _blocked(
                _base_dossier(publication_id=publication_id, settings=settings, now=now),
                "PUBLICATION_NOT_FOUND",
            )

        request_fingerprint = request_fingerprint_for(publication) if publication.publication_fingerprint else None
        authorization = _latest_authorization_status(db, publication.id, now)
        attempts_count = db.scalar(
            select(func.count()).select_from(PublicationAttempt).where(PublicationAttempt.publication_id == publication.id)
        )
        reconciliation_count = db.scalar(
            select(func.count())
            .select_from(PublicationReconciliationEvent)
            .where(PublicationReconciliationEvent.publication_id == publication.id)
        )
        source_image = db.get(ProductImage, publication.source_image_id) if publication.source_image_id else None
        dossier = _base_dossier(
            publication_id=publication.id,
            settings=settings,
            now=now,
            publication=publication,
            request_fingerprint=request_fingerprint,
            authorization=authorization,
        )
        dossier["attempts"] = {"count": int(attempts_count), "known_pinterest_pin": bool(publication.pinterest_pin_id)}
        dossier["reconciliation"] = {"count": int(reconciliation_count or 0)}
        dossier["media"]["provenance_present"] = bool(source_image and source_image.source_url)

        if _status(publication.status) != PublicationStatus.SCHEDULED.value:
            return _blocked(dossier, "INVALID_PUBLICATION_STATE")
        if int(attempts_count):
            return _blocked(dossier, "PRIOR_ATTEMPT_EXISTS")
        if int(reconciliation_count or 0):
            return _blocked(dossier, "RECONCILIATION_HISTORY_EXISTS")
        if publication.pinterest_pin_id:
            return _blocked(dossier, "KNOWN_PIN_EXISTS")
        if not source_image:
            return _blocked(dossier, "SOURCE_IMAGE_INVALID")

        manual = manual_structural_readiness(db, publication, now=now, require_due=False, dispatch_provider="buffer")
        if not manual["ready"]:
            return _blocked(dossier, manual["status"])
        dossier["pinterest_destination"]["local_binding_valid"] = True

        quality = validate_publication_quality(db, publication, dispatch_provider="buffer")
        dossier["quality"] = {
            "status": quality["status"],
            "policy_version": quality.get("policy_version"),
        }
        if quality["status"] != "PASS":
            return _blocked(dossier, "QUALITY_WARNING" if quality["status"] == "WARNING" else "QUALITY_FAILED")

        duplicate = evaluate_publication_duplicates(db, publication)
        dossier["duplicate"] = {"status": duplicate["status"]}
        if duplicate["status"] != SAFE_TO_CONTINUE:
            return _blocked(dossier, duplicate["status"])

        if not dossier["media"]["structurally_valid"]:
            return _blocked(dossier, "MEDIA_NOT_PUBLISHABLE")
        if not _buffer_identifier_valid(settings.buffer_organization_id):
            return _blocked(dossier, "BUFFER_ORGANIZATION_REQUIRED")
        if not _buffer_identifier_valid(settings.buffer_pinterest_channel_id):
            return _blocked(dossier, "BUFFER_CHANNEL_REQUIRED")
        if settings.buffer_api_base not in ALLOWED_BUFFER_API_BASES:
            return _blocked(dossier, "BUFFER_API_BASE_INVALID")

        try:
            payload = build_pinterest_payload(publication, settings)
            payload.to_input()
        except BufferConfigurationError as exc:
            return _blocked(dossier, str(exc) or "BUFFER_PAYLOAD_INVALID")

        return dossier | {
            "status": "STATIC_CANDIDATE_READY",
            "live_execution": "LOCKED",
            "buffer": _buffer_dossier(settings),
            "protected_gates": _protected_gates(settings),
        }
