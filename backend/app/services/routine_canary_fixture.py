from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select

from app.core.config import Settings, get_settings
from app.models.domain import PinPublication, PublicationStatus
from app.models.routine_publishing import (
    RoutineDispatchPermit,
    RoutinePublishingControl,
    RoutinePublishingRun,
)
from app.services.pinterest_publisher import normalize_persisted_utc
from app.services.publication_scheduler import schedule
from app.services.routine_activation_readiness import (
    _persisted_routing_current,
    routine_activation_candidate_snapshot,
)
from app.services.routine_dispatch_authorization import create_permit
from app.services.routine_offline_preflight import build_routine_offline_evidence
from app.services.routine_operational_readiness import routine_readiness_snapshot
from app.services.routine_pinterest_scheduler import scheduler_status
from app.services.routine_publishing_control import get_control


CONFIRMATION_TEXT_VERSION = "ROUTINE_DRY_RUN_CANARY_FIXTURE_V1"


class RoutineCanaryFixtureError(RuntimeError):
    pass


def _count(db, model, *criteria) -> int:
    return int(db.scalar(select(func.count()).select_from(model).where(*criteria)) or 0)


def _assert_static_safety(db, settings: Settings, control, scheduler: dict) -> None:
    if control is None or control.state != "PAUSED":
        raise RoutineCanaryFixtureError("ROUTINE_CONTROL_NOT_PAUSED")
    required_false = {
        "publishing_enabled": settings.publishing_enabled,
        "buffer_publishing_enabled": settings.buffer_publishing_enabled,
        "routine_pinterest_worker_enabled": settings.routine_pinterest_worker_enabled,
        "routine_buffer_dispatch_enabled": settings.routine_buffer_dispatch_enabled,
        "routine_pinterest_scheduler_enabled": settings.routine_pinterest_scheduler_enabled,
        "routine_autonomous_authorization_enabled": settings.routine_autonomous_authorization_enabled,
        "pinterest_autonomous_generation_enabled": settings.pinterest_autonomous_generation_enabled,
        "pinterest_autonomous_execution_enabled": settings.pinterest_autonomous_execution_enabled,
        "pinterest_autonomous_board_ensure_enabled": settings.pinterest_autonomous_board_ensure_enabled,
        "pinterest_write_scope_enabled": settings.pinterest_write_scope_enabled,
        "pinterest_board_write_scope_enabled": settings.pinterest_board_write_scope_enabled,
        "pinterest_board_provisioning_enabled": settings.pinterest_board_provisioning_enabled,
        "buffer_single_pin_pilot_enabled": settings.buffer_single_pin_pilot_enabled,
        "pinterest_single_pin_pilot_enabled": settings.pinterest_single_pin_pilot_enabled,
    }
    unsafe = sorted(key for key, value in required_false.items() if value is not False)
    if unsafe:
        raise RoutineCanaryFixtureError("UNSAFE_RUNTIME_GATE_STATE")
    if settings.routine_pinterest_dry_run is not True:
        raise RoutineCanaryFixtureError("ROUTINE_DRY_RUN_CONFIG_REQUIRED")
    if settings.routine_pinterest_batch_size != 1:
        raise RoutineCanaryFixtureError("ROUTINE_DRY_RUN_BATCH_SIZE_MUST_BE_ONE")
    if settings.routine_pinterest_daily_write_limit != 1:
        raise RoutineCanaryFixtureError("ROUTINE_DRY_RUN_DAILY_LIMIT_MUST_BE_ONE")
    if any(bool(scheduler.get(key)) for key in ("enabled", "started", "task_running", "tick_running", "lease_held")):
        raise RoutineCanaryFixtureError("ROUTINE_SCHEDULER_NOT_DORMANT")
    if scheduler.get("lease_supported") is not True:
        raise RoutineCanaryFixtureError("ROUTINE_DISTRIBUTED_LEASE_UNSUPPORTED")
    if _count(db, RoutinePublishingRun, RoutinePublishingRun.status == "RUNNING"):
        raise RoutineCanaryFixtureError("ROUTINE_WORKER_ALREADY_RUNNING")


def _pre_counts(db, now: datetime) -> dict:
    return {
        "due": _count(
            db, PinPublication,
            PinPublication.status == PublicationStatus.SCHEDULED,
            PinPublication.scheduled_for <= now,
        ),
        "future": _count(
            db, PinPublication,
            PinPublication.status == PublicationStatus.SCHEDULED,
            PinPublication.scheduled_for > now,
        ),
        "active_permits": _count(
            db, RoutineDispatchPermit,
            RoutineDispatchPermit.status == "ACTIVE",
        ),
        "publish_unknown": _count(
            db, PinPublication,
            PinPublication.status == PublicationStatus.PUBLISH_UNKNOWN,
        ),
    }


def prepare_atomic_dry_run_canary_fixture(
    db,
    *,
    publication_id: str,
    actor: str,
    settings: Settings | None = None,
    now: datetime | None = None,
    scheduler_snapshot: dict | None = None,
) -> dict:
    """Atomically materialize one provider-free DRY_RUN canary from an APPROVED snapshot.

    The function performs no provider/network work and never starts a routine run.
    The existing immutable publication snapshot is scheduled due; no creative,
    approval, destination, or publication identity is synthesized here.
    """
    if not actor:
        raise RoutineCanaryFixtureError("ACTOR_REQUIRED")
    settings = settings or get_settings()
    now = normalize_persisted_utc(now or datetime.now(timezone.utc))
    scheduler = scheduler_snapshot or scheduler_status(settings)

    try:
        # Serialize all fixture attempts on the singleton control row.
        control = db.scalar(
            select(RoutinePublishingControl)
            .where(RoutinePublishingControl.id == "default")
            .with_for_update()
        )
        if control is None:
            raise RoutineCanaryFixtureError("ROUTINE_CONTROL_NOT_INITIALIZED")
        _assert_static_safety(db, settings, control, scheduler)

        operational = routine_readiness_snapshot(db, settings=settings, now=now)
        critical = sorted(
            str(alert.get("code"))
            for alert in operational.get("alerts", [])
            if alert.get("severity") == "critical"
        )
        if operational.get("ready") is not True or critical:
            raise RoutineCanaryFixtureError("CRITICAL_OPERATIONAL_ALERT_PRESENT")

        before = _pre_counts(db, now)
        if before != {"due": 0, "future": 0, "active_permits": 0, "publish_unknown": 0}:
            raise RoutineCanaryFixtureError("CANARY_FIXTURE_PRECONDITION_COUNTS_NOT_ZERO")

        existing_candidates = routine_activation_candidate_snapshot(db, now=now)
        if any(int(existing_candidates.get(key, 0) or 0) for key in (
            "due_count", "active_permit_count", "valid_permit_count",
            "routing_current_count", "eligible_count",
        )):
            raise RoutineCanaryFixtureError("CANARY_FIXTURE_ALREADY_PRESENT")

        publication = db.scalar(
            select(PinPublication)
            .where(PinPublication.id == publication_id)
            .with_for_update()
        )
        if publication is None:
            raise RoutineCanaryFixtureError("CANARY_SOURCE_PUBLICATION_NOT_FOUND")
        if publication.status != PublicationStatus.APPROVED or publication.scheduled_for is not None:
            raise RoutineCanaryFixtureError("CANARY_SOURCE_PUBLICATION_NOT_APPROVED")
        if not _persisted_routing_current(db, publication):
            raise RoutineCanaryFixtureError("PERSISTED_PINTEREST_ROUTING_STALE")

        # One transaction: schedule, create permit, validate offline evidence,
        # prove exact candidate cardinality, then commit once.
        schedule(db, publication, now, commit=False)
        permit = create_permit(db, publication, actor=actor, now=now, commit=False)
        evidence = build_routine_offline_evidence(db, publication, permit=permit, now=now)
        if evidence.external_requests != 0:
            raise RoutineCanaryFixtureError("CANARY_EXTERNAL_REQUEST_INVARIANT_FAILED")

        candidates = routine_activation_candidate_snapshot(db, now=now)
        expected = {
            "due_count": 1,
            "active_permit_count": 1,
            "valid_permit_count": 1,
            "routing_current_count": 1,
            "eligible_count": 1,
        }
        if any(int(candidates.get(key, 0) or 0) != value for key, value in expected.items()):
            raise RoutineCanaryFixtureError("CANARY_FIXTURE_POSTCONDITION_COUNT_MISMATCH")
        if candidates.get("invalid_reasons") != {}:
            raise RoutineCanaryFixtureError("CANARY_FIXTURE_POSTCONDITION_INVALID")
        if candidates.get("eligible_publication_ids") != [publication.id]:
            raise RoutineCanaryFixtureError("CANARY_FIXTURE_IDENTITY_MISMATCH")
        if _pre_counts(db, now)["publish_unknown"] != 0:
            raise RoutineCanaryFixtureError("PUBLISH_UNKNOWN_PRESENT")
        if control.state != "PAUSED":
            raise RoutineCanaryFixtureError("ROUTINE_CONTROL_DRIFT")

        db.commit()
        return {
            "status": "PREPARED",
            "publication_id": publication.id,
            "control_state": "PAUSED",
            "dry_run": True,
            "external_requests": 0,
            "counts": {key: int(candidates[key]) for key in expected},
            "invalid_reasons": {},
        }
    except Exception:
        db.rollback()
        raise
