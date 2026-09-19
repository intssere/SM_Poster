from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.models.domain import PinPublication, PublicationStatus
from app.models.routine_publishing import RoutinePublishingControl, RoutinePublishingRun
from app.services.routine_operational_readiness import (
    derive_operational_alerts,
    routine_readiness_snapshot,
)


NOW = datetime(2026, 9, 19, 16, 0, tzinfo=timezone.utc)


def _settings(**overrides):
    values = {
        "database_url": "sqlite:///:memory:",
        "routine_pinterest_scheduler_enabled": False,
        "routine_pinterest_scheduler_interval_seconds": 300,
        "routine_pinterest_worker_enabled": False,
        "routine_buffer_dispatch_enabled": False,
        "routine_pinterest_dry_run": True,
        "routine_pinterest_batch_size": 1,
        "routine_pinterest_daily_write_limit": 1,
        "routine_claim_stale_seconds": 900,
    }
    values.update(overrides)
    return Settings(**values)


def _scheduler(**overrides):
    values = {
        "enabled": False,
        "interval_seconds": 300,
        "started": False,
        "task_running": False,
        "tick_running": False,
        "last_tick_started_at": None,
        "last_tick_completed_at": None,
        "last_error": None,
        "lease_required": False,
        "lease_backend": "postgresql_advisory_lock",
        "lease_supported": True,
        "lease_role": "disabled",
        "lease_held": False,
        "last_lease_status": "NOT_ATTEMPTED",
        "last_lease_acquired_at": None,
        "last_lease_lost_at": None,
        "last_lease_error": None,
    }
    values.update(overrides)
    return values


def _metrics(**overrides):
    values = {
        "control_state": "PAUSED",
        "pause_reason": "OPERATOR_PAUSE",
        "worker_enabled": False,
        "buffer_dispatch_enabled": False,
        "dry_run": True,
        "daily_write_limit": 1,
        "daily_provider_write_count": 0,
        "stale_seconds": 900,
        "active_running_run_count": 0,
        "stale_running_run_count": 0,
        "publishing_count": 0,
        "stale_publishing_count": 0,
        "publish_unknown_count": 0,
        "due_backlog": 0,
        "scheduler": _scheduler(),
    }
    values.update(overrides)
    return values


def _codes(alerts):
    return [item["code"] for item in alerts]


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    db.add(RoutinePublishingControl(
        id="default",
        state="PAUSED",
        pause_reason="OPERATOR_PAUSE",
        paused_at=NOW,
        paused_by="test",
    ))
    db.commit()
    return db


def test_healthy_paused_baseline_is_ready_and_has_no_alerts():
    db = _db()
    snapshot = routine_readiness_snapshot(
        db,
        settings=_settings(),
        scheduler_snapshot=_scheduler(),
        now=NOW,
    )
    assert snapshot["ready"] is True
    assert snapshot["alerts"] == []
    assert snapshot["severity_counts"] == {"critical": 0, "warning": 0, "info": 0}
    assert snapshot["metrics"]["control_state"] == "PAUSED"
    assert snapshot["metrics"]["daily_provider_write_count"] == 0
    db.close()


def test_publish_unknown_stale_and_multiple_run_alert_families():
    alerts = derive_operational_alerts(_metrics(
        publish_unknown_count=2,
        active_running_run_count=2,
        stale_running_run_count=1,
        stale_publishing_count=3,
    ))
    assert {
        "PUBLISH_UNKNOWN_PRESENT",
        "MULTIPLE_RUNNING_RUNS",
        "STALE_RUNNING_RUN",
        "STALE_PUBLISHING_PUBLICATION",
    }.issubset(set(_codes(alerts)))
    assert all(
        item["severity"] == "critical"
        for item in alerts
        if item["code"] in {
            "PUBLISH_UNKNOWN_PRESENT",
            "MULTIPLE_RUNNING_RUNS",
            "STALE_RUNNING_RUN",
            "STALE_PUBLISHING_PUBLICATION",
        }
    )


def test_daily_limit_reached_and_exceeded_are_distinct():
    reached = derive_operational_alerts(_metrics(daily_provider_write_count=1))
    assert "DAILY_WRITE_LIMIT_REACHED" in _codes(reached)
    assert "DAILY_WRITE_LIMIT_EXCEEDED" not in _codes(reached)

    exceeded = derive_operational_alerts(_metrics(daily_provider_write_count=2))
    assert "DAILY_WRITE_LIMIT_EXCEEDED" in _codes(exceeded)
    assert "DAILY_WRITE_LIMIT_REACHED" not in _codes(exceeded)


def test_scheduler_mismatch_alert_families():
    enabled_not_running = derive_operational_alerts(_metrics(
        worker_enabled=False,
        scheduler=_scheduler(enabled=True, started=True, task_running=False),
    ))
    assert "SCHEDULER_ENABLED_TASK_NOT_RUNNING" in _codes(enabled_not_running)
    assert "SCHEDULER_ENABLED_WORKER_DISABLED" in _codes(enabled_not_running)

    disabled_running = derive_operational_alerts(_metrics(
        scheduler=_scheduler(enabled=False, started=False, task_running=True),
    ))
    assert "SCHEDULER_DISABLED_TASK_RUNNING" in _codes(disabled_running)


def test_live_control_requires_aligned_autonomous_gates():
    alerts = derive_operational_alerts(_metrics(control_state="LIVE"))
    match = next(item for item in alerts if item["code"] == "LIVE_CONTROL_UNSAFE_CONFIGURATION")
    assert match["severity"] == "critical"
    assert match["context"]["mismatches"] == [
        "scheduler_disabled",
        "worker_disabled",
        "buffer_dispatch_disabled",
        "dry_run_enabled",
    ]

    aligned = derive_operational_alerts(_metrics(
        control_state="LIVE",
        worker_enabled=True,
        buffer_dispatch_enabled=True,
        dry_run=False,
        scheduler=_scheduler(enabled=True, started=True, task_running=True),
    ))
    assert "LIVE_CONTROL_UNSAFE_CONFIGURATION" not in _codes(aligned)


def test_due_backlog_is_informational():
    alerts = derive_operational_alerts(_metrics(due_backlog=4))
    match = next(item for item in alerts if item["code"] == "DUE_BACKLOG_PRESENT")
    assert match["severity"] == "info"
    assert match["context"] == {"count": 4}


def test_alert_order_is_deterministic_by_severity_then_code():
    alerts = derive_operational_alerts(_metrics(
        publish_unknown_count=1,
        stale_running_run_count=1,
        daily_provider_write_count=1,
        due_backlog=2,
        scheduler=_scheduler(enabled=True, task_running=False),
    ))
    pairs = [(item["severity"], item["code"]) for item in alerts]
    rank = {"critical": 0, "warning": 1, "info": 2}
    assert pairs == sorted(pairs, key=lambda item: (rank[item[0]], item[1]))


def test_exact_stale_boundary_is_alerted_for_run_and_publication():
    db = _db()
    cutoff = NOW - timedelta(seconds=900)
    db.add(RoutinePublishingRun(
        id="run-stale",
        mode="DRY_RUN",
        status="RUNNING",
        started_at=cutoff,
        heartbeat_at=cutoff,
    ))
    db.add(PinPublication(
        id="pub-stale",
        draft_id="draft-x",
        creative_id="creative-x",
        publication_fingerprint="a" * 64,
        status=PublicationStatus.PUBLISHING,
        attempt_started_at=cutoff,
    ))
    db.commit()

    snapshot = routine_readiness_snapshot(
        db,
        settings=_settings(),
        scheduler_snapshot=_scheduler(),
        now=NOW,
    )
    assert snapshot["metrics"]["stale_running_run_count"] == 1
    assert snapshot["metrics"]["stale_publishing_count"] == 1
    assert "STALE_RUNNING_RUN" in _codes(snapshot["alerts"])
    assert "STALE_PUBLISHING_PUBLICATION" in _codes(snapshot["alerts"])
    assert snapshot["ready"] is False
    db.close()


def test_readiness_snapshot_is_read_only_and_excludes_credentials():
    db = _db()
    settings = _settings(
        buffer_api_key="super-secret",
        buffer_organization_id="org-secret",
        buffer_pinterest_channel_id="channel-secret",
    )
    before = (
        len(db.new),
        len(db.dirty),
        len(db.deleted),
        db.get(RoutinePublishingControl, "default").state,
    )
    snapshot = routine_readiness_snapshot(
        db,
        settings=settings,
        scheduler_snapshot=_scheduler(),
        now=NOW,
    )
    after = (
        len(db.new),
        len(db.dirty),
        len(db.deleted),
        db.get(RoutinePublishingControl, "default").state,
    )
    assert after == before
    rendered = repr(snapshot)
    assert "super-secret" not in rendered
    assert "org-secret" not in rendered
    assert "channel-secret" not in rendered
    db.close()


def test_scheduler_lease_readiness_alerts_and_standby_health():
    unsupported = derive_operational_alerts(_metrics(
        scheduler=_scheduler(
            enabled=True,
            started=True,
            task_running=True,
            lease_required=True,
            lease_supported=False,
            lease_role="error",
            last_lease_status="UNSUPPORTED_BACKEND",
            last_lease_error="POSTGRESQL_REQUIRED",
        ),
    ))
    assert "SCHEDULER_LEASE_UNSUPPORTED" in _codes(unsupported)
    assert "SCHEDULER_LEASE_ERROR" in _codes(unsupported)

    disabled_held = derive_operational_alerts(_metrics(
        scheduler=_scheduler(
            enabled=False,
            lease_held=True,
            lease_role="leader",
            last_lease_status="ACQUIRED",
        ),
    ))
    assert "SCHEDULER_DISABLED_LEASE_HELD" in _codes(disabled_held)

    impossible_leader = derive_operational_alerts(_metrics(
        scheduler=_scheduler(
            enabled=True,
            started=True,
            task_running=True,
            lease_required=True,
            lease_supported=True,
            lease_role="leader",
            lease_held=False,
            last_lease_status="ACQUIRED",
        ),
    ))
    assert "SCHEDULER_LEADER_WITHOUT_LEASE" in _codes(impossible_leader)

    standby = derive_operational_alerts(_metrics(
        worker_enabled=True,
        scheduler=_scheduler(
            enabled=True,
            started=True,
            task_running=True,
            lease_required=True,
            lease_supported=True,
            lease_role="standby",
            lease_held=False,
            last_lease_status="STANDBY",
        ),
    ))
    assert not any(code.startswith("SCHEDULER_LEASE") for code in _codes(standby))
    assert "SCHEDULER_LEADER_WITHOUT_LEASE" not in _codes(standby)
