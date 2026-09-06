"""Read-only final Buffer pilot execution-readiness gate.

Phase 3B does not dispatch, contact providers, infer credential authority, or
mutate persistence. It composes Phase 3A static certification with explicit
external evidence for a future separately authorized one-write pilot.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.domain import PinPublication
from app.services.buffer_pilot_certification import certify_buffer_pilot_candidate
from app.services.buffer_single_pin_pilot import validate_pilot
from app.services.publication_dispatch_authorization import (
    AUTHORIZATION_TTL,
    latest_authorization,
    manual_structural_readiness,
    validate_authorization,
)
from app.services.publication_scheduler import request_fingerprint_for
from app.services.pinterest_publisher import normalize_persisted_utc

LIVE_EXECUTION_LOCKED = "LIVE_EXECUTION_LOCKED"
FINAL_EXECUTION_READY = "FINAL_EXECUTION_READY"
FRESH_PERSISTED_READ_UNAVAILABLE = "FRESH_PERSISTED_READ_UNAVAILABLE"


@dataclass(frozen=True)
class BufferPilotExecutionEvidence:
    """Non-secret external evidence for one exact Buffer pilot candidate."""

    publication_id: str
    publication_fingerprint: str
    request_fingerprint: str
    buffer_organization_id: str
    buffer_pinterest_channel_id: str
    board_service_id: str
    media_url: str
    observed_at: datetime | None
    write_credential_authorized: bool = False
    provider_destination_live_verified: bool = False
    media_live_fetch_verified: bool = False


def _locked(
    *,
    static_status: str | None,
    lock_reason: str,
    publication: PinPublication | None = None,
    requested_publication_id: str | None = None,
    request_fingerprint: str | None = None,
    evidence: BufferPilotExecutionEvidence | None = None,
) -> dict[str, Any]:
    return {
        "static_status": static_status,
        "execution_status": LIVE_EXECUTION_LOCKED,
        "lock_reason": lock_reason,
        "publication_id": publication.id if publication else requested_publication_id,
        "publication_fingerprint": publication.publication_fingerprint if publication else None,
        "request_fingerprint": request_fingerprint,
        "evidence": _safe_evidence(evidence),
    }


def _ready(
    *,
    publication: PinPublication,
    request_fingerprint: str,
    evidence: BufferPilotExecutionEvidence,
) -> dict[str, Any]:
    return {
        "static_status": "STATIC_CANDIDATE_READY",
        "execution_status": FINAL_EXECUTION_READY,
        "lock_reason": None,
        "publication_id": publication.id,
        "publication_fingerprint": publication.publication_fingerprint,
        "request_fingerprint": request_fingerprint,
        "evidence": _safe_evidence(evidence),
    }


def _safe_evidence(evidence: BufferPilotExecutionEvidence | None) -> dict[str, Any] | None:
    if evidence is None:
        return None
    result = asdict(evidence)
    observed_at = result.get("observed_at")
    if isinstance(observed_at, datetime):
        result["observed_at"] = normalize_persisted_utc(observed_at).isoformat()
    elif observed_at is not None:
        result["observed_at"] = "<invalid>"
    return result


def _evidence_freshness_error(evidence: BufferPilotExecutionEvidence, now: datetime) -> str | None:
    if evidence.observed_at is not None and not isinstance(evidence.observed_at, datetime):
        return "EXECUTION_EVIDENCE_INVALID"
    try:
        observed_at = normalize_persisted_utc(evidence.observed_at)
    except (AttributeError, TypeError, ValueError):
        return "EXECUTION_EVIDENCE_INVALID"
    if observed_at is None or observed_at > now:
        return "EXECUTION_EVIDENCE_STALE"
    if now - observed_at > AUTHORIZATION_TTL:
        return "EXECUTION_EVIDENCE_STALE"
    return None


def _evidence_matches(
    evidence: BufferPilotExecutionEvidence,
    *,
    publication: PinPublication,
    request_fingerprint: str,
    settings: Settings,
) -> bool:
    return (
        evidence.publication_id == publication.id
        and evidence.publication_fingerprint == publication.publication_fingerprint
        and evidence.request_fingerprint == request_fingerprint
        and evidence.buffer_organization_id == settings.buffer_organization_id
        and evidence.buffer_pinterest_channel_id == settings.buffer_pinterest_channel_id
        and evidence.board_service_id == publication.pinterest_board_id_snapshot
        and evidence.media_url == publication.media_url_snapshot
    )


def _is_sqlite_memory_engine(bind: Engine) -> bool:
    if bind.dialect.name != "sqlite":
        return False
    database = bind.url.database
    return database in (None, "", ":memory:")


def _fresh_read_bind(db: Session) -> Engine | None:
    bind = db.get_bind()
    if isinstance(bind, Connection):
        return None
    if not isinstance(bind, Engine):
        return None
    if _is_sqlite_memory_engine(bind) and db.in_transaction():
        return None
    return bind


def evaluate_buffer_pilot_execution_readiness(
    db: Session,
    publication_id: str,
    *,
    evidence: BufferPilotExecutionEvidence | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate final execution readiness without side effects or provider I/O."""
    settings = settings or get_settings()
    now = normalize_persisted_utc(now or datetime.now(timezone.utc))
    with db.no_autoflush:
        bind = _fresh_read_bind(db)
        if bind is None:
            requested_publication_id = publication_id if isinstance(publication_id, str) and publication_id.strip() else None
            return _locked(
                static_status=None,
                lock_reason=FRESH_PERSISTED_READ_UNAVAILABLE,
                requested_publication_id=requested_publication_id,
                evidence=evidence,
            ) | {"static": None}
        fresh_db = Session(bind=bind, expire_on_commit=False)
        try:
            with fresh_db.no_autoflush:
                return _evaluate_buffer_pilot_execution_readiness_from_persistence(
                    fresh_db,
                    publication_id,
                    evidence=evidence,
                    settings=settings,
                    now=now,
                )
        finally:
            fresh_db.close()


def _evaluate_buffer_pilot_execution_readiness_from_persistence(
    db: Session,
    publication_id: str,
    *,
    evidence: BufferPilotExecutionEvidence | None,
    settings: Settings,
    now: datetime,
) -> dict[str, Any]:
    requested_publication_id = publication_id if isinstance(publication_id, str) and publication_id.strip() else None
    static = certify_buffer_pilot_candidate(db, publication_id, settings=settings, now=now)
    if static["status"] != "STATIC_CANDIDATE_READY":
        return _locked(
            static_status=static["status"],
            lock_reason="STATIC_CERTIFICATION_BLOCKED",
            requested_publication_id=requested_publication_id,
            evidence=evidence,
        ) | {"static": static}

    publication = db.get(PinPublication, publication_id)
    request_fingerprint = request_fingerprint_for(publication)

    manual = manual_structural_readiness(db, publication, now=now, require_due=True)
    if not manual["ready"]:
        return _locked(
            static_status=static["status"],
            lock_reason=manual["status"],
            publication=publication,
            request_fingerprint=request_fingerprint,
            evidence=evidence,
        ) | {"static": static}

    ok, pilot_reason = validate_pilot(db, publication, settings, attempt=None)
    if not ok:
        return _locked(
            static_status=static["status"],
            lock_reason=pilot_reason,
            publication=publication,
            request_fingerprint=request_fingerprint,
            evidence=evidence,
        ) | {"static": static}

    authorization = latest_authorization(db, publication.id)
    auth = validate_authorization(db, publication, authorization, now=now)
    if not auth["valid"]:
        return _locked(
            static_status=static["status"],
            lock_reason=auth["status"],
            publication=publication,
            request_fingerprint=request_fingerprint,
            evidence=evidence,
        ) | {"static": static}

    if evidence is None:
        return _locked(
            static_status=static["status"],
            lock_reason="EXECUTION_EVIDENCE_REQUIRED",
            publication=publication,
            request_fingerprint=request_fingerprint,
        ) | {"static": static}
    freshness_error = _evidence_freshness_error(evidence, now)
    if freshness_error is not None:
        return _locked(
            static_status=static["status"],
            lock_reason=freshness_error,
            publication=publication,
            request_fingerprint=request_fingerprint,
            evidence=evidence,
        ) | {"static": static}
    if not _evidence_matches(
        evidence,
        publication=publication,
        request_fingerprint=request_fingerprint,
        settings=settings,
    ):
        return _locked(
            static_status=static["status"],
            lock_reason="EXECUTION_EVIDENCE_MISMATCH",
            publication=publication,
            request_fingerprint=request_fingerprint,
            evidence=evidence,
        ) | {"static": static}
    if evidence.write_credential_authorized is not True:
        return _locked(
            static_status=static["status"],
            lock_reason="WRITE_CREDENTIAL_NOT_AUTHORIZED",
            publication=publication,
            request_fingerprint=request_fingerprint,
            evidence=evidence,
        ) | {"static": static}
    if evidence.provider_destination_live_verified is not True:
        return _locked(
            static_status=static["status"],
            lock_reason="PROVIDER_DESTINATION_NOT_VERIFIED",
            publication=publication,
            request_fingerprint=request_fingerprint,
            evidence=evidence,
        ) | {"static": static}
    if evidence.media_live_fetch_verified is not True:
        return _locked(
            static_status=static["status"],
            lock_reason="MEDIA_NOT_LIVE_VERIFIED",
            publication=publication,
            request_fingerprint=request_fingerprint,
            evidence=evidence,
        ) | {"static": static}

    return _ready(
        publication=publication,
        request_fingerprint=request_fingerprint,
        evidence=evidence,
    ) | {"static": static}
