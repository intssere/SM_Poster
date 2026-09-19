from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from typing import Literal

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.models.domain import (
    PinPublication,
    PinterestBoard,
    PinterestConnection,
    PublicationStatus,
)
from app.models.routine_publishing import RoutinePublishingRun
from app.services.pinterest_publisher import normalize_persisted_utc
from app.services.routine_dispatch_authorization import active_permit, validate_permit
from app.services.routine_operational_readiness import routine_readiness_snapshot
from app.services.routine_pinterest_scheduler import scheduler_status
from app.services.routine_publishing_control import daily_provider_write_count, get_control

ActivationMode = Literal["DRY_RUN", "LIVE"]
BUFFER_API_BASE = "https://api.buffer.com"


def _utc(value: datetime | None) -> datetime:
    return normalize_persisted_utc(value or datetime.now(timezone.utc))


def _check(code: str, passed: bool, message: str, **context) -> dict:
    return {
        "code": code,
        "passed": bool(passed),
        "blocking": not bool(passed),
        "message": message,
        "context": context,
    }


def _buffer_configured(settings: Settings) -> bool:
    key = settings.buffer_api_key
    return bool(
        isinstance(key, str)
        and key.strip()
        and settings.buffer_api_base.rstrip("/") == BUFFER_API_BASE
        and settings.buffer_organization_id
        and settings.buffer_pinterest_channel_id
    )


def _persisted_routing_current(db, publication: PinPublication) -> bool:
    if not publication.pinterest_connection_id or not publication.pinterest_board_record_id:
        return False
    connection = db.get(PinterestConnection, publication.pinterest_connection_id)
    board = db.get(PinterestBoard, publication.pinterest_board_record_id)
    return bool(
        connection
        and board
        and connection.provider == "pinterest"
        and connection.status == "CONNECTED"
        and board.connection_id == connection.id
        and board.is_active
        and board.is_eligible
        and board.last_synced_at is not None
        and connection.boards_last_synced_at is not None
        and board.last_synced_at == connection.boards_last_synced_at
        and publication.pinterest_board_id_snapshot
        and board.external_board_id == publication.pinterest_board_id_snapshot
    )


def routine_activation_candidate_snapshot(db, *, now: datetime) -> dict:
    due = list(db.scalars(
        select(PinPublication)
        .where(
            PinPublication.status == PublicationStatus.SCHEDULED,
            PinPublication.scheduled_for <= now,
        )
        .order_by(PinPublication.scheduled_for, PinPublication.id)
    ).all())

    active_permit_count = 0
    valid_permit_count = 0
    routing_current_count = 0
    eligible_ids: list[str] = []
    invalid_reasons: Counter[str] = Counter()

    for publication in due:
        permit = active_permit(db, publication.id)
        if permit is None:
            invalid_reasons["ROUTINE_PERMIT_REQUIRED"] += 1
            continue
        active_permit_count += 1
        try:
            validated = validate_permit(
                db,
                publication,
                permit,
                now=now,
                require_due=True,
            )
        except Exception:
            invalid_reasons["ROUTINE_PERMIT_VALIDATION_ERROR"] += 1
            continue
        if validated.get("valid") is not True:
            invalid_reasons[str(validated.get("status") or "ROUTINE_PERMIT_INVALID")] += 1
            continue
        valid_permit_count += 1
        if not _persisted_routing_current(db, publication):
            invalid_reasons["PERSISTED_PINTEREST_ROUTING_STALE"] += 1
            continue
        routing_current_count += 1
        eligible_ids.append(publication.id)

    return {
        "due_count": len(due),
        "active_permit_count": active_permit_count,
        "valid_permit_count": valid_permit_count,
        "routing_current_count": routing_current_count,
        "eligible_count": len(eligible_ids),
        "eligible_publication_ids": sorted(eligible_ids),
        "invalid_reasons": {
            key: invalid_reasons[key]
            for key in sorted(invalid_reasons)
        },
    }


def _activation_plan(target_mode: ActivationMode) -> dict:
    if target_mode == "DRY_RUN":
        return {
            "control_state": "DRY_RUN",
            "routine_pinterest_scheduler_enabled": True,
            "routine_pinterest_worker_enabled": True,
            "routine_buffer_dispatch_enabled": False,
            "routine_pinterest_dry_run": True,
            "routine_pinterest_batch_size": 1,
            "routine_pinterest_daily_write_limit": 1,
        }
    return {
        "control_state": "LIVE",
        "routine_pinterest_scheduler_enabled": True,
        "routine_pinterest_worker_enabled": True,
        "routine_buffer_dispatch_enabled": True,
        "routine_pinterest_dry_run": False,
        "routine_pinterest_batch_size": 1,
        "routine_pinterest_daily_write_limit": 1,
    }


def _activation_fingerprint(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def routine_activation_readiness(
    db,
    *,
    target_mode: ActivationMode,
    settings: Settings | None = None,
    scheduler_snapshot: dict | None = None,
    operational_readiness: dict | None = None,
    candidate_snapshot: dict | None = None,
    now: datetime | None = None,
) -> dict:
    if target_mode not in {"DRY_RUN", "LIVE"}:
        raise ValueError("INVALID_ACTIVATION_MODE")

    settings = settings or get_settings()
    now = _utc(now)
    sched = scheduler_snapshot if scheduler_snapshot is not None else scheduler_status(settings)
    operational = operational_readiness if operational_readiness is not None else routine_readiness_snapshot(
        db,
        settings=settings,
        scheduler_snapshot=sched,
        now=now,
    )
    candidates = candidate_snapshot if candidate_snapshot is not None else routine_activation_candidate_snapshot(
        db,
        now=now,
    )

    control = get_control(db, create=False)
    active_run = db.scalar(
        select(RoutinePublishingRun.id)
        .where(RoutinePublishingRun.status == "RUNNING")
        .limit(1)
    )
    day_start = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    write_count = daily_provider_write_count(db, day_start=day_start)

    critical_alert_codes = sorted(
        alert["code"]
        for alert in operational.get("alerts", [])
        if alert.get("severity") == "critical"
    )

    dormant_scheduler = bool(
        sched.get("enabled") is False
        and sched.get("started") is False
        and sched.get("task_running") is False
        and sched.get("tick_running") is False
    )

    checks = [
        _check(
            "CONTROL_INITIALIZED_AND_PAUSED",
            control is not None and control.state == "PAUSED",
            "Routine control must exist and be PAUSED before activation.",
            current_state=control.state if control else None,
        ),
        _check(
            "SCHEDULER_DORMANT",
            dormant_scheduler,
            "Scheduler must be disabled and stopped before activation.",
        ),
        _check(
            "WORKER_DORMANT",
            settings.routine_pinterest_worker_enabled is False,
            "Routine worker must be disabled before activation.",
        ),
        _check(
            "BUFFER_DISPATCH_DORMANT",
            settings.routine_buffer_dispatch_enabled is False,
            "Routine Buffer dispatch must be disabled before activation.",
        ),
        _check(
            "DRY_RUN_BASELINE",
            settings.routine_pinterest_dry_run is True,
            "Dormant configuration must retain dry-run=true.",
        ),
        _check(
            "BATCH_SIZE_ONE",
            settings.routine_pinterest_batch_size == 1,
            "Initial autonomous activation requires batch size 1.",
            configured=settings.routine_pinterest_batch_size,
        ),
        _check(
            "DAILY_WRITE_LIMIT_ONE",
            settings.routine_pinterest_daily_write_limit == 1,
            "Initial autonomous activation requires daily write limit 1.",
            configured=settings.routine_pinterest_daily_write_limit,
        ),
        _check(
            "NO_ACTIVE_ROUTINE_RUN",
            active_run is None,
            "No routine publishing run may be RUNNING at activation time.",
        ),
        _check(
            "NO_CRITICAL_OPERATIONAL_ALERTS",
            not critical_alert_codes,
            "Task #45/46 operational readiness must have no critical alerts.",
            critical_alert_codes=critical_alert_codes,
        ),
        _check(
            "DISTRIBUTED_LEASE_SUPPORTED",
            bool(sched.get("lease_supported")),
            "PostgreSQL advisory-lock scheduler leadership must be supported.",
            lease_backend=sched.get("lease_backend"),
        ),
        _check(
            "DISTRIBUTED_LEASE_NOT_HELD",
            not bool(sched.get("lease_held")),
            "No scheduler lease may already be held before activation.",
            lease_role=sched.get("lease_role"),
            last_lease_status=sched.get("last_lease_status"),
        ),
        _check(
            "BUFFER_CONFIGURATION_PRESENT",
            _buffer_configured(settings),
            "Buffer credential and routing configuration must be present.",
        ),
        _check(
            "DUE_PUBLICATION_PRESENT",
            int(candidates.get("due_count", 0)) > 0,
            "At least one SCHEDULED publication must be due.",
            due_count=int(candidates.get("due_count", 0)),
        ),
        _check(
            "PERMIT_VALID_CANDIDATE_PRESENT",
            int(candidates.get("valid_permit_count", 0)) > 0,
            "At least one due publication must have a currently valid routine permit.",
            valid_permit_count=int(candidates.get("valid_permit_count", 0)),
        ),
        _check(
            "PERSISTED_ROUTING_CURRENT",
            int(candidates.get("eligible_count", 0)) > 0,
            "At least one permit-valid candidate must have current persisted Pinterest routing.",
            eligible_count=int(candidates.get("eligible_count", 0)),
        ),
    ]

    if target_mode == "LIVE":
        checks.extend([
            _check(
                "LIVE_PUBLISHING_ENABLED",
                settings.publishing_enabled is True,
                "Global publishing must be enabled for LIVE activation.",
            ),
            _check(
                "LIVE_BUFFER_PUBLISHING_ENABLED",
                settings.buffer_publishing_enabled is True,
                "Buffer publishing must be enabled for LIVE activation.",
            ),
            _check(
                "LIVE_DAILY_QUOTA_AVAILABLE",
                write_count < settings.routine_pinterest_daily_write_limit,
                "Daily provider-write quota must be available for LIVE activation.",
                current=write_count,
                limit=settings.routine_pinterest_daily_write_limit,
            ),
        ])

    blockers = [item["code"] for item in checks if item["blocking"]]
    plan = _activation_plan(target_mode)
    safe_fingerprint_input = {
        "target_mode": target_mode,
        "plan": plan,
        "control_state": control.state if control else None,
        "scheduler": {
            key: sched.get(key)
            for key in (
                "enabled",
                "started",
                "task_running",
                "tick_running",
                "lease_required",
                "lease_backend",
                "lease_supported",
                "lease_role",
                "lease_held",
                "last_lease_status",
            )
        },
        "routine_settings": {
            "worker_enabled": settings.routine_pinterest_worker_enabled,
            "buffer_dispatch_enabled": settings.routine_buffer_dispatch_enabled,
            "dry_run": settings.routine_pinterest_dry_run,
            "batch_size": settings.routine_pinterest_batch_size,
            "daily_write_limit": settings.routine_pinterest_daily_write_limit,
            "publishing_enabled": settings.publishing_enabled,
            "buffer_publishing_enabled": settings.buffer_publishing_enabled,
        },
        "provider_write_count": write_count,
        "critical_alert_codes": critical_alert_codes,
        "candidate_ids": candidates.get("eligible_publication_ids", []),
        "candidate_counts": {
            key: candidates.get(key, 0)
            for key in (
                "due_count",
                "active_permit_count",
                "valid_permit_count",
                "routing_current_count",
                "eligible_count",
            )
        },
    }

    return {
        "target_mode": target_mode,
        "ready": not blockers,
        "checked_at": now,
        "blockers": blockers,
        "checks": checks,
        "candidate_summary": candidates,
        "provider_write_count": write_count,
        "activation_plan": plan,
        "activation_fingerprint": _activation_fingerprint(safe_fingerprint_input),
        "state_mutated": False,
        "provider_called": False,
    }
