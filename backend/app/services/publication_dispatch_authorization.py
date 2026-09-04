"""Trusted Task #39 manual dispatch authorization lifecycle.

No API route lives here.  Callers must pass a server-derived actor; client JSON
must never own ``authorized_by``.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.domain import (
    PinApproval,
    PinCreative,
    AuditLog,
    PinPublication,
    PinterestBoard,
    PinterestConnection,
    PublicationDispatchAuthorization,
    PublicationStatus,
)
from app.services.pinterest_publication_quality import (
    PINTEREST_QUALITY_V1,
    validate_publication_quality,
)
from app.services.pinterest_publisher import media_publishable, normalize_persisted_utc
from app.services.publication_duplicates import SAFE_TO_CONTINUE, evaluate_publication_duplicates

AUTHORIZATION_TTL = timedelta(minutes=15)
CONFIRMATION_TEXT_VERSION = "CONFIRM_DISPATCH_V1"


class DispatchAuthorizationError(RuntimeError):
    """Bounded authorization/readiness failure."""


def _is_active_authorization_unique_violation(exc: IntegrityError) -> bool:
    """Recognize only the active-authorization uniqueness conflict.

    Driver metadata is preferred; SQLite's deterministic index message is
    supported for the isolated test database.  Other integrity failures must
    propagate to callers unchanged.
    """
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    if getattr(diag, "constraint_name", None) == "uq_publication_dispatch_authorizations_active":
        return True
    message = str(orig or exc)
    return message == (
        "UNIQUE constraint failed: "
        "publication_dispatch_authorizations.publication_id"
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _status(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _safe_snapshot(value: Any) -> Any:
    """Return a JSON-safe deep copy built from deterministic service output."""
    return deepcopy(value)


def active_authorization(db: Session, publication_id: str) -> PublicationDispatchAuthorization | None:
    return db.scalar(
        select(PublicationDispatchAuthorization)
        .where(
            PublicationDispatchAuthorization.publication_id == publication_id,
            PublicationDispatchAuthorization.status == "ACTIVE",
        )
        .order_by(PublicationDispatchAuthorization.authorized_at.desc())
        .limit(1)
    )


def latest_authorization(db: Session, publication_id: str) -> PublicationDispatchAuthorization | None:
    return db.scalar(
        select(PublicationDispatchAuthorization)
        .where(PublicationDispatchAuthorization.publication_id == publication_id)
        .order_by(PublicationDispatchAuthorization.authorized_at.desc(), PublicationDispatchAuthorization.created_at.desc())
        .limit(1)
    )


def expire_stale_active_authorizations(db: Session, publication_id: str, *, now: datetime | None = None) -> int:
    now = normalize_persisted_utc(now or _now())
    result = db.execute(
        update(PublicationDispatchAuthorization)
        .execution_options(synchronize_session=False)
        .where(
            PublicationDispatchAuthorization.publication_id == publication_id,
            PublicationDispatchAuthorization.status == "ACTIVE",
            PublicationDispatchAuthorization.expires_at <= now,
        )
        .values(status="EXPIRED")
    )
    return int(result.rowcount or 0)


def revoke_authorization(
    db: Session,
    authorization: PublicationDispatchAuthorization,
    *,
    actor: str,
    reason: str,
    now: datetime | None = None,
) -> PublicationDispatchAuthorization:
    if not actor:
        raise DispatchAuthorizationError("ACTOR_REQUIRED")
    if not reason or not reason.strip() or len(reason) > 255 or any(ord(c) < 32 or ord(c) == 127 for c in reason):
        raise DispatchAuthorizationError("INVALID_REVOKE_REASON")
    if authorization.status != "ACTIVE":
        raise DispatchAuthorizationError("AUTHORIZATION_NOT_ACTIVE")
    authorization.status = "REVOKED"
    authorization.revoked_at = normalize_persisted_utc(now or _now())
    authorization.revoke_reason = reason[:255]
    db.add(AuditLog(actor=actor, action="PUBLICATION_DISPATCH_AUTHORIZATION_REVOKED", entity_type="PublicationDispatchAuthorization", entity_id=authorization.id, metadata_json={"authorization_id": authorization.id}))
    db.commit()
    return authorization


def _base_snapshot_complete(publication: PinPublication) -> bool:
    return all(
        (
            publication.publication_fingerprint,
            publication.text_fingerprint,
            publication.pinterest_connection_id,
            publication.pinterest_board_record_id,
            publication.pinterest_board_id_snapshot,
            publication.title_snapshot,
            publication.description_snapshot,
            publication.alt_text_snapshot,
            publication.destination_url,
            publication.utm_url,
            publication.media_url_snapshot,
            publication.creative_id,
            publication.source_image_id,
            publication.creative_fingerprint,
            publication.template_id,
            publication.template_key,
            publication.template_version is not None,
        )
    )


def manual_structural_readiness(
    db: Session,
    publication: PinPublication,
    *,
    now: datetime | None = None,
    expected_publication_state: PublicationStatus = PublicationStatus.SCHEDULED,
) -> dict[str, Any]:
    """Pure manual-readiness checks. Does not inspect/modify authorization."""
    now = normalize_persisted_utc(now or _now())
    scheduled_for = normalize_persisted_utc(publication.scheduled_for)
    if _status(publication.status) != expected_publication_state.value:
        return {"status": "INVALID_PUBLICATION_STATE", "ready": False}
    if not scheduled_for or scheduled_for > now:
        return {"status": "NOT_DUE", "ready": False}
    if not _base_snapshot_complete(publication):
        return {"status": "INCOMPLETE_SNAPSHOT", "ready": False}

    connection = db.get(PinterestConnection, publication.pinterest_connection_id)
    if not connection or connection.status != "CONNECTED":
        return {"status": "DESTINATION_INVALID", "ready": False}
    board = db.get(PinterestBoard, publication.pinterest_board_record_id)
    if not board or board.connection_id != connection.id or not board.is_active or not board.is_eligible:
        return {"status": "DESTINATION_INVALID", "ready": False}
    if board.external_board_id != publication.pinterest_board_id_snapshot:
        return {"status": "DESTINATION_INVALID", "ready": False}

    approval = db.get(PinApproval, publication.approval_id) if publication.approval_id else None
    if (
        not approval
        or approval.decision != "APPROVED"
        or approval.draft_id != publication.draft_id
        or approval.revision_id != publication.revision_id
        or approval.creative_id != publication.creative_id
    ):
        return {"status": "APPROVAL_INVALID", "ready": False}

    creative = db.get(PinCreative, publication.creative_id)
    if (
        not creative
        or creative.draft_id != publication.draft_id
        or creative.source_image_id != publication.source_image_id
        or creative.creative_fingerprint != publication.creative_fingerprint
        or creative.rendered_url != publication.media_url_snapshot
    ):
        return {"status": "CREATIVE_INVALID", "ready": False}
    if not media_publishable(publication.media_url_snapshot):
        return {"status": "MEDIA_NOT_PUBLISHABLE", "ready": False}
    return {"status": "READY_FOR_MANUAL_DISPATCH", "ready": True}


def provider_readiness(db: Session, publication: PinPublication) -> dict[str, Any]:
    if not get_settings().publishing_enabled:
        return {"status": "PUBLISHING_DISABLED", "ready": False, "live_provider_write_enabled": False}
    connection = db.get(PinterestConnection, publication.pinterest_connection_id)
    if not connection or connection.status != "CONNECTED" or "pins:write" not in (connection.granted_scopes or []):
        return {"status": "PUBLISHING_SCOPE_REQUIRED", "ready": False, "live_provider_write_enabled": False}
    return {"status": "READY", "ready": True, "live_provider_write_enabled": True}


def readiness_result(db: Session, publication: PinPublication, *, now: datetime | None = None) -> dict[str, Any]:
    now = normalize_persisted_utc(now or _now())
    quality = validate_publication_quality(db, publication)
    duplicate = evaluate_publication_duplicates(db, publication)
    manual = manual_structural_readiness(db, publication, now=now)
    if manual["ready"] and quality["status"] != "PASS":
        manual = {"status": "QUALITY_WARNING" if quality["status"] == "WARNING" else "QUALITY_FAILED", "ready": False}
    if manual["ready"] and duplicate["status"] != SAFE_TO_CONTINUE:
        manual = {
            "status": "UNKNOWN_OUTCOME_BLOCKED" if duplicate["status"] == "UNKNOWN_OUTCOME_BLOCKS_RETRY" else "DUPLICATE_BLOCKED",
            "ready": False,
        }
    auth = latest_authorization(db, publication.id)
    authorization = {"status": "AUTHORIZATION_REQUIRED", "authorization_id": None}
    if auth:
        expires_at = normalize_persisted_utc(auth.expires_at)
        if auth.status == "REVOKED":
            authorization = {"status": "AUTHORIZATION_REVOKED", "authorization_id": auth.id}
        elif auth.status == "CONSUMED":
            authorization = {"status": "AUTHORIZATION_CONSUMED", "authorization_id": auth.id}
        elif auth.status == "EXPIRED":
            authorization = {"status": "AUTHORIZATION_EXPIRED", "authorization_id": auth.id}
        elif auth.status == "ACTIVE" and expires_at and expires_at <= now:
            authorization = {"status": "AUTHORIZATION_EXPIRED", "authorization_id": auth.id}
        elif auth.status == "ACTIVE":
            authorization = {"status": "ACTIVE", "authorization_id": auth.id, "expires_at": auth.expires_at}
    provider = provider_readiness(db, publication)
    return {
        "manual_status": manual["status"],
        "manual_ready": manual["ready"],
        "provider_status": provider["status"],
        "live_provider_write_enabled": provider["live_provider_write_enabled"],
        "quality": quality,
        "duplicate": duplicate,
        "authorization": authorization,
    }


def create_authorization(
    db: Session,
    publication: PinPublication,
    *,
    actor: str,
    now: datetime | None = None,
) -> PublicationDispatchAuthorization:
    now = normalize_persisted_utc(now or _now())
    if not actor:
        raise DispatchAuthorizationError("AUTHORIZED_BY_REQUIRED")
    expire_stale_active_authorizations(db, publication.id, now=now)
    db.flush()

    quality = validate_publication_quality(db, publication)
    if quality["status"] != "PASS":
        raise DispatchAuthorizationError("QUALITY_WARNING" if quality["status"] == "WARNING" else "QUALITY_FAILED")
    duplicate = evaluate_publication_duplicates(db, publication)
    if duplicate["status"] != SAFE_TO_CONTINUE:
        raise DispatchAuthorizationError(duplicate["status"])
    manual = manual_structural_readiness(db, publication, now=now)
    if not manual["ready"]:
        raise DispatchAuthorizationError(manual["status"])
    if active_authorization(db, publication.id):
        raise DispatchAuthorizationError("ACTIVE_AUTHORIZATION_EXISTS")

    authorization = PublicationDispatchAuthorization(
        publication_id=publication.id,
        authorized_by=actor,
        authorized_at=now,
        publication_fingerprint=publication.publication_fingerprint,
        quality_policy_version=PINTEREST_QUALITY_V1,
        quality_snapshot=_safe_snapshot(quality),
        readiness_snapshot=_safe_snapshot(manual),
        duplicate_snapshot=_safe_snapshot(duplicate),
        confirmation_text_version=CONFIRMATION_TEXT_VERSION,
        expires_at=now + AUTHORIZATION_TTL,
        status="ACTIVE",
    )
    db.add(authorization)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if _is_active_authorization_unique_violation(exc):
            raise DispatchAuthorizationError("ACTIVE_AUTHORIZATION_EXISTS") from None
        raise
    db.refresh(authorization)
    return authorization


def validate_authorization_snapshot_binding(
    db: Session,
    publication: PinPublication,
    authorization: PublicationDispatchAuthorization,
    *,
    now: datetime | None = None,
    expected_publication_state: PublicationStatus = PublicationStatus.SCHEDULED,
) -> dict[str, Any]:
    now = normalize_persisted_utc(now or _now())
    if authorization.publication_fingerprint != publication.publication_fingerprint:
        return {"valid": False, "status": "AUTHORIZATION_MISMATCH"}
    quality = validate_publication_quality(db, publication)
    duplicate = evaluate_publication_duplicates(db, publication)
    manual = manual_structural_readiness(db, publication, now=now, expected_publication_state=expected_publication_state)
    if authorization.quality_policy_version != PINTEREST_QUALITY_V1:
        return {"valid": False, "status": "AUTHORIZATION_MISMATCH"}
    if quality != authorization.quality_snapshot:
        return {"valid": False, "status": "AUTHORIZATION_MISMATCH"}
    if duplicate != authorization.duplicate_snapshot:
        return {"valid": False, "status": "AUTHORIZATION_MISMATCH"}
    if manual != authorization.readiness_snapshot:
        return {"valid": False, "status": "AUTHORIZATION_MISMATCH"}
    return {"valid": True, "status": "SNAPSHOT_BOUND", "quality": quality, "duplicate": duplicate, "manual": manual}


def validate_authorization(
    db: Session,
    publication: PinPublication,
    authorization: PublicationDispatchAuthorization | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = normalize_persisted_utc(now or _now())
    if not authorization:
        return {"valid": False, "status": "AUTHORIZATION_REQUIRED"}
    if authorization.publication_id != publication.id:
        return {"valid": False, "status": "AUTHORIZATION_MISMATCH"}
    if authorization.status != "ACTIVE":
        return {"valid": False, "status": f"AUTHORIZATION_{authorization.status}"}
    expires_at = normalize_persisted_utc(authorization.expires_at)
    if not expires_at or expires_at <= now:
        return {"valid": False, "status": "AUTHORIZATION_EXPIRED"}
    result = validate_authorization_snapshot_binding(db, publication, authorization, now=now)
    if not result["valid"]:
        return result
    result["status"] = "ACTIVE"
    return result
