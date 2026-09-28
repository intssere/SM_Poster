"""Provider-free certification and single-use reservation for a targeted Buffer pilot."""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.config import Settings
from app.models.domain import PinPublication, PublicationAttempt, PublicationStatus
from app.models.routine_publishing import (
    RoutineAttemptBoundary, RoutinePublishingControl, RoutinePublishingRun,
)
from app.services import routine_certified_canary as canary
from app.services.routine_canary_fixture import RoutineCanaryFixtureError
from app.services.routine_publishing_control import daily_provider_write_count


PREFLIGHT_CONTRACT_VERSION = "ROUTINE_CERTIFIED_LIVE_PREFLIGHT_V1"
CONFIRMATION_TEXT_VERSION = "ROUTINE_CERTIFIED_LIVE_ONCE_V1"
RECOVERY_CONFIRMATION_TEXT_VERSION = "ROUTINE_CERTIFIED_LIVE_RECOVER_V1"
RECEIPT_TTL = timedelta(minutes=5)


class CertifiedLiveError(RuntimeError):
    pass


def _clock(now):
    return (lambda: now) if now is not None else (lambda: datetime.now(timezone.utc))


def _live_safety(db, settings: Settings, control, scheduler):
    if control is None or control.state != "PAUSED":
        raise RoutineCanaryFixtureError("ROUTINE_CONTROL_NOT_PAUSED")
    if settings.publishing_enabled is not True or settings.buffer_publishing_enabled is not True:
        raise RoutineCanaryFixtureError("CERTIFIED_LIVE_PUBLISHING_DISABLED")
    disabled = (
        "routine_pinterest_worker_enabled", "routine_buffer_dispatch_enabled",
        "routine_pinterest_scheduler_enabled", "routine_autonomous_authorization_enabled",
        "pinterest_autonomous_generation_enabled", "pinterest_autonomous_execution_enabled",
        "pinterest_autonomous_board_ensure_enabled", "pinterest_board_provisioning_enabled",
        "pinterest_write_scope_enabled", "pinterest_board_write_scope_enabled",
        "buffer_single_pin_pilot_enabled", "pinterest_single_pin_pilot_enabled",
    )
    if any(getattr(settings, name) is not False for name in disabled):
        raise RoutineCanaryFixtureError("CERTIFIED_LIVE_UNRELATED_WRITE_GATE_ENABLED")
    if not settings.buffer_organization_id or not settings.buffer_pinterest_channel_id:
        raise RoutineCanaryFixtureError("CERTIFIED_LIVE_BUFFER_DESTINATION_REQUIRED")
    if settings.routine_pinterest_dry_run is not True:
        raise RoutineCanaryFixtureError("ROUTINE_DRY_RUN_CONFIG_REQUIRED")
    if settings.routine_pinterest_batch_size != 1 or settings.routine_pinterest_daily_write_limit != 1:
        raise RoutineCanaryFixtureError("CERTIFIED_LIVE_LIMIT_MUST_BE_ONE")
    if any(scheduler.get(key) is not False for key in
           ("enabled", "started", "task_running", "tick_running", "lease_held")):
        raise RoutineCanaryFixtureError("ROUTINE_SCHEDULER_NOT_DORMANT")
    if scheduler.get("lease_supported") is not True:
        raise RoutineCanaryFixtureError("ROUTINE_DISTRIBUTED_LEASE_UNSUPPORTED")
    if db.scalar(select(RoutinePublishingRun.id).where(RoutinePublishingRun.status == "RUNNING").limit(1)):
        raise RoutineCanaryFixtureError("ROUTINE_WORKER_ALREADY_RUNNING")


def _live_state(db, publication, permit, settings, now):
    if publication is None:
        raise canary.CertifiedCanaryError("CERTIFIED_LIVE_PUBLICATION_REQUIRED")
    if db.scalar(select(PinPublication.id).where(
        PinPublication.status == PublicationStatus.PUBLISH_UNKNOWN,
    ).limit(1)):
        raise canary.CertifiedCanaryError("CERTIFIED_LIVE_UNKNOWN_BLOCKED")
    if db.scalar(select(PublicationAttempt.id).where(
        PublicationAttempt.publication_id == publication.id,
    ).limit(1)):
        raise canary.CertifiedCanaryError("CERTIFIED_LIVE_ALREADY_ATTEMPTED")
    day_start = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    if daily_provider_write_count(db, day_start=day_start) != 0:
        raise canary.CertifiedCanaryError("CERTIFIED_LIVE_DAILY_LIMIT_REACHED")
    if not publication.pinterest_board_id_snapshot or not permit.pinterest_board_record_id:
        raise canary.CertifiedCanaryError("CERTIFIED_LIVE_BOARD_REQUIRED")


def _evaluate(db, *, publication_id, settings, now, scheduler_snapshot, locked):
    return canary._evaluate_prerequisites(
        db, publication_id=publication_id, settings=settings, now=now,
        scheduler_snapshot=scheduler_snapshot, locked=locked,
        safety_check=_live_safety, additional_check=_live_state,
    )


def _binding(publication_id, permit_id, request_fingerprint, state, settings):
    publication, board = state["publication"], state["board"]
    if not publication or not board:
        raise CertifiedLiveError("CERTIFIED_LIVE_BOARD_REQUIRED")
    return {
        "publication_id": publication_id,
        "permit_id": permit_id,
        "publication_fingerprint": publication["publication_fingerprint"],
        "request_fingerprint": request_fingerprint,
        "pinterest_board_record_id": publication["pinterest_board_record_id"],
        "pinterest_board_id": publication["pinterest_board_id_snapshot"],
        "buffer_organization_id": settings.buffer_organization_id,
        "buffer_pinterest_channel_id": settings.buffer_pinterest_channel_id,
    }


def _signed_state(state, settings):
    return hmac.new(canary._signing_key(settings), canary._bytes(state), hashlib.sha256).hexdigest()


def _release():
    identity = canary._release_identity()
    if identity is None:
        raise CertifiedLiveError("CERTIFIED_LIVE_RELEASE_IDENTITY_REQUIRED")
    return identity


def preflight_certified_live(
    db, *, publication_id: str, settings: Settings, now: datetime | None = None,
    scheduler_snapshot: dict | None = None,
) -> dict:
    key = canary._signing_key(settings)
    release = _release()
    permit_id, request_fp, state, issued = _evaluate(
        db, publication_id=publication_id, settings=settings,
        now=_clock(now), scheduler_snapshot=scheduler_snapshot, locked=False,
    )
    payload = {
        "contract_version": PREFLIGHT_CONTRACT_VERSION,
        **_binding(publication_id, permit_id, request_fp, state, settings),
        "prerequisite_fingerprint": _signed_state(state, settings),
        "release_identity": release,
        "issued_at": issued.isoformat(),
        "expires_at": (issued + RECEIPT_TTL).isoformat(),
    }
    return {**payload, "signature": hmac.new(key, canary._bytes(payload), hashlib.sha256).hexdigest()}


def _check_receipt(receipt, publication_id, settings, now):
    fields = {
        "contract_version", "publication_id", "permit_id", "publication_fingerprint",
        "request_fingerprint", "pinterest_board_record_id", "pinterest_board_id",
        "buffer_organization_id", "buffer_pinterest_channel_id", "prerequisite_fingerprint",
        "release_identity", "issued_at", "expires_at", "signature",
    }
    if not isinstance(receipt, dict) or set(receipt) != fields:
        raise CertifiedLiveError("CERTIFIED_LIVE_RECEIPT_REQUIRED")
    if receipt["contract_version"] != PREFLIGHT_CONTRACT_VERSION or receipt["publication_id"] != publication_id:
        raise CertifiedLiveError("CERTIFIED_LIVE_RECEIPT_IDENTITY_MISMATCH")
    if any(not isinstance(receipt[k], str) or not receipt[k] for k in fields - {"release_identity"}):
        raise CertifiedLiveError("CERTIFIED_LIVE_RECEIPT_MALFORMED")
    if any(len(receipt[k]) != 64 or any(c not in "0123456789abcdef" for c in receipt[k])
           for k in ("request_fingerprint", "publication_fingerprint", "prerequisite_fingerprint", "signature")):
        raise CertifiedLiveError("CERTIFIED_LIVE_RECEIPT_MALFORMED")
    if not isinstance(receipt["release_identity"], dict):
        raise CertifiedLiveError("CERTIFIED_LIVE_RELEASE_IDENTITY_REQUIRED")
    try:
        issued = datetime.fromisoformat(receipt["issued_at"])
        expires = datetime.fromisoformat(receipt["expires_at"])
        if issued.tzinfo is None or expires.tzinfo is None:
            raise ValueError("timezone required")
        payload = {key: value for key, value in receipt.items() if key != "signature"}
        expected = hmac.new(canary._signing_key(settings), canary._bytes(payload), hashlib.sha256).hexdigest()
    except (TypeError, ValueError, OverflowError, canary.CertifiedCanaryError):
        raise CertifiedLiveError("CERTIFIED_LIVE_RECEIPT_MALFORMED") from None
    if not hmac.compare_digest(expected, receipt["signature"]):
        raise CertifiedLiveError("CERTIFIED_LIVE_SIGNATURE_INVALID")
    if expires - issued != RECEIPT_TTL or issued > now or now >= expires:
        raise CertifiedLiveError("CERTIFIED_LIVE_RECEIPT_EXPIRED")
    if receipt["release_identity"] != _release():
        raise CertifiedLiveError("CERTIFIED_LIVE_RELEASE_DRIFT")


def reserve_certified_live(
    db, *, publication_id: str, settings: Settings, receipt: dict, actor: str,
    now: datetime | None = None, scheduler_snapshot: dict | None = None,
) -> RoutinePublishingRun:
    if not actor:
        raise CertifiedLiveError("CERTIFIED_LIVE_ACTOR_REQUIRED")
    clock = _clock(now)
    _check_receipt(receipt, publication_id, settings, clock())
    try:
        permit_id, request_fp, state, completed = _evaluate(
            db, publication_id=publication_id, settings=settings, now=clock,
            scheduler_snapshot=scheduler_snapshot, locked=True,
        )
        _check_receipt(receipt, publication_id, settings, completed)
        if any(receipt[key] != value for key, value in
               _binding(publication_id, permit_id, request_fp, state, settings).items()):
            raise CertifiedLiveError("CERTIFIED_LIVE_BINDING_DRIFT")
        if not hmac.compare_digest(receipt["prerequisite_fingerprint"], _signed_state(state, settings)):
            raise CertifiedLiveError("CERTIFIED_LIVE_STATE_DRIFT")
        control = db.get(RoutinePublishingControl, "default")
        if not control or control.state != "PAUSED":
            raise CertifiedLiveError("CERTIFIED_LIVE_CONTROL_DRIFT")
        control.state = "LIVE"
        control.pause_reason = None
        control.paused_at = None
        control.paused_by = None
        run = RoutinePublishingRun(
            mode="LIVE", started_at=completed, heartbeat_at=completed, status="RUNNING",
            metadata_json={
                "certified_live_version": PREFLIGHT_CONTRACT_VERSION,
                "publication_id": publication_id, "permit_id": permit_id,
                "request_fingerprint": request_fp, "actor": actor[:255],
                "receipt_sha256": hashlib.sha256(canary._bytes(receipt)).hexdigest(),
            },
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        return run
    except Exception:
        db.rollback()
        raise


def recover_certified_live(db, *, actor: str, stale_seconds: int, now: datetime | None = None):
    """Operator-only fail-closed recovery; never dispatches or reactivates a permit."""
    if not actor:
        raise CertifiedLiveError("CERTIFIED_LIVE_ACTOR_REQUIRED")
    now = now or datetime.now(timezone.utc)
    try:
        control = db.scalar(select(RoutinePublishingControl).where(
            RoutinePublishingControl.id == "default",
        ).with_for_update())
        if control is None or control.state not in {"LIVE", "PAUSED"}:
            raise CertifiedLiveError("CERTIFIED_LIVE_NOT_ARMED")
        run = db.scalar(select(RoutinePublishingRun).order_by(
            RoutinePublishingRun.started_at.desc(),
        ).limit(1).with_for_update())
        meta = run.metadata_json if run else {}
        if not meta or meta.get("certified_live_version") != PREFLIGHT_CONTRACT_VERSION:
            raise CertifiedLiveError("CERTIFIED_LIVE_RUN_REQUIRED")
        heartbeat = run.heartbeat_at or run.started_at
        if run.status == "RUNNING" and heartbeat and heartbeat > now - timedelta(seconds=stale_seconds):
            raise CertifiedLiveError("CERTIFIED_LIVE_RUN_NOT_STALE")
        publication = db.get(PinPublication, meta["publication_id"])
        if control.state == "PAUSED" and (publication is None or publication.status != PublicationStatus.PUBLISHING):
            raise CertifiedLiveError("CERTIFIED_LIVE_NOT_ARMED")
        attempt = db.scalar(select(PublicationAttempt).where(
            PublicationAttempt.publication_id == meta["publication_id"],
        ).order_by(PublicationAttempt.started_at.desc()).limit(1))
        if publication and publication.status == PublicationStatus.PUBLISHING:
            boundary = db.scalar(select(RoutineAttemptBoundary).where(
                RoutineAttemptBoundary.attempt_id == attempt.id,
            )) if attempt else None
            unknown = not boundary or boundary.provider_mutation_started_at is not None
            publication.status = PublicationStatus.PUBLISH_UNKNOWN if unknown else PublicationStatus.PUBLISH_FAILED
            publication.error_code = "CERTIFIED_LIVE_STALE_AFTER_BOUNDARY" if unknown else "CERTIFIED_LIVE_STALE_BEFORE_BOUNDARY"
            if attempt and attempt.status == "STARTED":
                attempt.status = "UNKNOWN" if unknown else "FAILED"
                attempt.error_code = publication.error_code
                if not unknown:
                    attempt.completed_at = now
        if run.status == "RUNNING":
            run.status = "FAILED"
            run.error_code = "CERTIFIED_LIVE_STALE"
            run.completed_at = now
            run.heartbeat_at = now
        control.state = "PAUSED"
        control.pause_reason = "CERTIFIED_LIVE_RECOVERED"
        control.paused_at = now
        control.paused_by = actor[:255]
        db.commit()
        return {"status": "PAUSED", "publication_id": meta["publication_id"]}
    except Exception:
        db.rollback()
        raise