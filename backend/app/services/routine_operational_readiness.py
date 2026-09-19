from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.core.config import Settings, get_settings
from app.models.domain import PinPublication, PublicationStatus
from app.models.routine_publishing import RoutinePublishingRun
from app.services.routine_pinterest_scheduler import scheduler_status
from app.services.routine_publishing_control import daily_provider_write_count, get_control


SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _alert(code: str, severity: str, message: str, **context):
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "context": context,
    }


def derive_operational_alerts(metrics: dict) -> list[dict]:
    alerts: list[dict] = []

    if metrics["publish_unknown_count"] > 0:
        alerts.append(_alert(
            "PUBLISH_UNKNOWN_PRESENT",
            "critical",
            "One or more publications have an unknown provider outcome.",
            count=metrics["publish_unknown_count"],
        ))

    if metrics["active_running_run_count"] > 1:
        alerts.append(_alert(
            "MULTIPLE_RUNNING_RUNS",
            "critical",
            "More than one routine publishing run is marked RUNNING.",
            count=metrics["active_running_run_count"],
        ))

    if metrics["stale_running_run_count"] > 0:
        alerts.append(_alert(
            "STALE_RUNNING_RUN",
            "critical",
            "A routine publishing run heartbeat is stale.",
            count=metrics["stale_running_run_count"],
            stale_seconds=metrics["stale_seconds"],
        ))

    if metrics["stale_publishing_count"] > 0:
        alerts.append(_alert(
            "STALE_PUBLISHING_PUBLICATION",
            "critical",
            "A publication has remained PUBLISHING beyond the stale threshold.",
            count=metrics["stale_publishing_count"],
            stale_seconds=metrics["stale_seconds"],
        ))

    write_count = metrics["daily_provider_write_count"]
    write_limit = metrics["daily_write_limit"]
    if write_count > write_limit:
        alerts.append(_alert(
            "DAILY_WRITE_LIMIT_EXCEEDED",
            "critical",
            "Provider mutation count exceeds the configured daily limit.",
            count=write_count,
            limit=write_limit,
        ))
    elif write_count == write_limit:
        alerts.append(_alert(
            "DAILY_WRITE_LIMIT_REACHED",
            "warning",
            "Provider mutation count has reached the configured daily limit.",
            count=write_count,
            limit=write_limit,
        ))

    scheduler = metrics["scheduler"]
    if scheduler["enabled"] and not scheduler["task_running"]:
        alerts.append(_alert(
            "SCHEDULER_ENABLED_TASK_NOT_RUNNING",
            "critical",
            "The routine scheduler is enabled but its task is not running.",
        ))
    if not scheduler["enabled"] and scheduler["task_running"]:
        alerts.append(_alert(
            "SCHEDULER_DISABLED_TASK_RUNNING",
            "critical",
            "The routine scheduler task is running while scheduler configuration is disabled.",
        ))
    if scheduler["enabled"] and not metrics["worker_enabled"]:
        alerts.append(_alert(
            "SCHEDULER_ENABLED_WORKER_DISABLED",
            "warning",
            "The scheduler is enabled while the routine worker is disabled.",
        ))

    if metrics["control_state"] == "LIVE":
        unsafe = []
        if not scheduler["enabled"]:
            unsafe.append("scheduler_disabled")
        if not metrics["worker_enabled"]:
            unsafe.append("worker_disabled")
        if not metrics["buffer_dispatch_enabled"]:
            unsafe.append("buffer_dispatch_disabled")
        if metrics["dry_run"]:
            unsafe.append("dry_run_enabled")
        if unsafe:
            alerts.append(_alert(
                "LIVE_CONTROL_UNSAFE_CONFIGURATION",
                "critical",
                "Routine control is LIVE while required autonomous publishing gates are not aligned.",
                mismatches=unsafe,
            ))

    if metrics["due_backlog"] > 0:
        alerts.append(_alert(
            "DUE_BACKLOG_PRESENT",
            "info",
            "One or more scheduled publications are due.",
            count=metrics["due_backlog"],
        ))

    return sorted(
        alerts,
        key=lambda item: (SEVERITY_ORDER[item["severity"]], item["code"]),
    )


def routine_readiness_snapshot(
    db,
    *,
    settings: Settings | None = None,
    scheduler_snapshot: dict | None = None,
    now: datetime | None = None,
) -> dict:
    settings = settings or get_settings()
    now = _utc(now) or datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=settings.routine_claim_stale_seconds)
    control = get_control(db, create=False)
    control_state = control.state if control else "PAUSED"
    pause_reason = control.pause_reason if control else "ROUTINE_CONTROL_NOT_INITIALIZED"

    running_runs = list(db.scalars(
        select(RoutinePublishingRun).where(RoutinePublishingRun.status == "RUNNING")
    ).all())
    stale_running = 0
    for run in running_runs:
        heartbeat = _utc(run.heartbeat_at or run.started_at)
        if heartbeat is None or heartbeat <= cutoff:
            stale_running += 1

    publishing_rows = list(db.scalars(
        select(PinPublication).where(PinPublication.status == PublicationStatus.PUBLISHING)
    ).all())
    stale_publishing = 0
    for publication in publishing_rows:
        started = _utc(publication.attempt_started_at)
        if started is None or started <= cutoff:
            stale_publishing += 1

    publish_unknown_count = int(db.scalar(
        select(func.count(PinPublication.id)).where(
            PinPublication.status == PublicationStatus.PUBLISH_UNKNOWN
        )
    ) or 0)
    due_backlog = int(db.scalar(
        select(func.count(PinPublication.id)).where(
            PinPublication.status == PublicationStatus.SCHEDULED,
            PinPublication.scheduled_for <= now,
        )
    ) or 0)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    writes = daily_provider_write_count(db, day_start=day_start)
    sched = scheduler_snapshot if scheduler_snapshot is not None else scheduler_status(settings)

    metrics = {
        "control_state": control_state,
        "pause_reason": pause_reason,
        "worker_enabled": settings.routine_pinterest_worker_enabled,
        "buffer_dispatch_enabled": settings.routine_buffer_dispatch_enabled,
        "dry_run": settings.routine_pinterest_dry_run,
        "daily_write_limit": settings.routine_pinterest_daily_write_limit,
        "daily_provider_write_count": writes,
        "stale_seconds": settings.routine_claim_stale_seconds,
        "active_running_run_count": len(running_runs),
        "stale_running_run_count": stale_running,
        "publishing_count": len(publishing_rows),
        "stale_publishing_count": stale_publishing,
        "publish_unknown_count": publish_unknown_count,
        "due_backlog": due_backlog,
        "scheduler": {
            "enabled": bool(sched.get("enabled")),
            "interval_seconds": sched.get("interval_seconds"),
            "started": bool(sched.get("started")),
            "task_running": bool(sched.get("task_running")),
            "tick_running": bool(sched.get("tick_running")),
            "last_tick_started_at": sched.get("last_tick_started_at"),
            "last_tick_completed_at": sched.get("last_tick_completed_at"),
            "last_error": sched.get("last_error"),
        },
    }
    alerts = derive_operational_alerts(metrics)
    severity_counts = {
        severity: sum(1 for alert in alerts if alert["severity"] == severity)
        for severity in ("critical", "warning", "info")
    }
    return {
        "ready": severity_counts["critical"] == 0,
        "checked_at": now,
        "severity_counts": severity_counts,
        "alerts": alerts,
        "metrics": metrics,
    }
