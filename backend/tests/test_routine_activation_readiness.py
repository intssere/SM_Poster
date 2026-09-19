from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.models.domain import PinPublication, PinterestBoard, PinterestConnection, PublicationStatus
from app.models.routine_publishing import RoutinePublishingControl, RoutinePublishingRun
from app.services import routine_activation_readiness as activation


NOW = datetime(2026, 9, 19, 18, 30, tzinfo=timezone.utc)


def _settings(**overrides):
    values = {
        "database_url": "sqlite:///:memory:",
        "buffer_api_key": "secret-buffer-key",
        "buffer_api_base": "https://api.buffer.com",
        "buffer_organization_id": "org-1",
        "buffer_pinterest_channel_id": "channel-1",
        "publishing_enabled": False,
        "buffer_publishing_enabled": False,
        "routine_pinterest_worker_enabled": False,
        "routine_buffer_dispatch_enabled": False,
        "routine_pinterest_dry_run": True,
        "routine_pinterest_batch_size": 1,
        "routine_pinterest_daily_write_limit": 1,
        "routine_pinterest_scheduler_enabled": False,
        "routine_pinterest_scheduler_interval_seconds": 300,
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
        "last_result": None,
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


def _operational(*, critical=None):
    critical = critical or []
    alerts = [
        {"code": code, "severity": "critical", "message": code, "context": {}}
        for code in critical
    ]
    return {
        "ready": not critical,
        "severity_counts": {
            "critical": len(critical),
            "warning": 0,
            "info": 0,
        },
        "alerts": alerts,
        "metrics": {},
    }


def _candidates(**overrides):
    values = {
        "due_count": 1,
        "active_permit_count": 1,
        "valid_permit_count": 1,
        "routing_current_count": 1,
        "eligible_count": 1,
        "eligible_publication_ids": ["pub-1"],
        "invalid_reasons": {},
    }
    values.update(overrides)
    return values


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    db.add(RoutinePublishingControl(
        id="default",
        state="PAUSED",
        pause_reason="ONE_SHOT_LIVE_COMPLETE",
        paused_at=NOW,
        paused_by="test",
    ))
    db.commit()
    return db


def _gate(db, mode="DRY_RUN", **kwargs):
    return activation.routine_activation_readiness(
        db,
        target_mode=mode,
        settings=kwargs.pop("settings", _settings()),
        scheduler_snapshot=kwargs.pop("scheduler_snapshot", _scheduler()),
        operational_readiness=kwargs.pop("operational_readiness", _operational()),
        candidate_snapshot=kwargs.pop("candidate_snapshot", _candidates()),
        now=NOW,
        **kwargs,
    )


def _codes(result):
    return [item["code"] for item in result["checks"] if item["blocking"]]


def test_safe_dry_run_activation_gate_is_ready_without_live_flags():
    db = _db()
    result = _gate(db, "DRY_RUN")
    assert result["ready"] is True
    assert result["blockers"] == []
    assert result["activation_plan"] == {
        "control_state": "DRY_RUN",
        "routine_pinterest_scheduler_enabled": True,
        "routine_pinterest_worker_enabled": True,
        "routine_buffer_dispatch_enabled": False,
        "routine_pinterest_dry_run": True,
        "routine_pinterest_batch_size": 1,
        "routine_pinterest_daily_write_limit": 1,
    }
    assert len(result["activation_fingerprint"]) == 64
    assert result["state_mutated"] is False
    assert result["provider_called"] is False
    db.close()


def test_safe_live_activation_gate_is_ready_when_live_flags_and_quota_allow(monkeypatch):
    db = _db()
    monkeypatch.setattr(activation, "daily_provider_write_count", lambda *args, **kwargs: 0)
    result = _gate(
        db,
        "LIVE",
        settings=_settings(
            publishing_enabled=True,
            buffer_publishing_enabled=True,
        ),
    )
    assert result["ready"] is True
    assert result["blockers"] == []
    assert result["activation_plan"]["control_state"] == "LIVE"
    assert result["activation_plan"]["routine_buffer_dispatch_enabled"] is True
    assert result["activation_plan"]["routine_pinterest_dry_run"] is False
    db.close()


def test_dormant_baseline_and_active_run_block_activation():
    db = _db()
    db.add(RoutinePublishingRun(
        id="run-1",
        mode="DRY_RUN",
        started_at=NOW,
        heartbeat_at=NOW,
        status="RUNNING",
    ))
    control = db.get(RoutinePublishingControl, "default")
    control.state = "DRY_RUN"
    db.commit()

    result = _gate(
        db,
        "DRY_RUN",
        settings=_settings(routine_pinterest_worker_enabled=True),
        scheduler_snapshot=_scheduler(enabled=True, started=True, task_running=True),
    )
    assert {
        "CONTROL_INITIALIZED_AND_PAUSED",
        "SCHEDULER_DORMANT",
        "WORKER_DORMANT",
        "NO_ACTIVE_ROUTINE_RUN",
    }.issubset(set(_codes(result)))
    assert result["ready"] is False
    db.close()


def test_critical_operational_and_lease_states_block():
    db = _db()
    result = _gate(
        db,
        "DRY_RUN",
        operational_readiness=_operational(critical=["PUBLISH_UNKNOWN_PRESENT", "STALE_RUNNING_RUN"]),
        scheduler_snapshot=_scheduler(
            lease_supported=False,
            lease_role="error",
            lease_held=True,
            last_lease_status="ERROR",
        ),
    )
    assert "NO_CRITICAL_OPERATIONAL_ALERTS" in _codes(result)
    assert "DISTRIBUTED_LEASE_SUPPORTED" in _codes(result)
    assert "DISTRIBUTED_LEASE_NOT_HELD" in _codes(result)
    assert result["ready"] is False
    db.close()


def test_missing_buffer_configuration_blocks_without_leaking_secret():
    db = _db()
    settings = _settings(
        buffer_api_key="super-secret-value",
        buffer_organization_id=None,
    )
    result = _gate(db, settings=settings)
    assert "BUFFER_CONFIGURATION_PRESENT" in _codes(result)
    rendered = repr(result)
    assert "super-secret-value" not in rendered
    assert "secret-buffer-key" not in rendered
    db.close()


def test_candidate_absence_invalid_permit_and_stale_route_block():
    db = _db()

    no_due = _gate(db, candidate_snapshot=_candidates(
        due_count=0,
        active_permit_count=0,
        valid_permit_count=0,
        routing_current_count=0,
        eligible_count=0,
        eligible_publication_ids=[],
        invalid_reasons={},
    ))
    assert "DUE_PUBLICATION_PRESENT" in _codes(no_due)
    assert "PERMIT_VALID_CANDIDATE_PRESENT" in _codes(no_due)
    assert "PERSISTED_ROUTING_CURRENT" in _codes(no_due)

    invalid_permit = _gate(db, candidate_snapshot=_candidates(
        active_permit_count=1,
        valid_permit_count=0,
        routing_current_count=0,
        eligible_count=0,
        eligible_publication_ids=[],
        invalid_reasons={"ROUTINE_PERMIT_SNAPSHOT_DRIFT": 1},
    ))
    assert "PERMIT_VALID_CANDIDATE_PRESENT" in _codes(invalid_permit)

    stale_route = _gate(db, candidate_snapshot=_candidates(
        valid_permit_count=1,
        routing_current_count=0,
        eligible_count=0,
        eligible_publication_ids=[],
        invalid_reasons={"PERSISTED_PINTEREST_ROUTING_STALE": 1},
    ))
    assert "PERMIT_VALID_CANDIDATE_PRESENT" not in _codes(stale_route)
    assert "PERSISTED_ROUTING_CURRENT" in _codes(stale_route)
    db.close()


def test_live_flags_quota_batch_and_limit_are_blocking(monkeypatch):
    db = _db()
    monkeypatch.setattr(activation, "daily_provider_write_count", lambda *args, **kwargs: 1)
    result = _gate(
        db,
        "LIVE",
        settings=_settings(
            publishing_enabled=False,
            buffer_publishing_enabled=False,
            routine_pinterest_batch_size=2,
            routine_pinterest_daily_write_limit=1,
        ),
    )
    assert {
        "BATCH_SIZE_ONE",
        "LIVE_PUBLISHING_ENABLED",
        "LIVE_BUFFER_PUBLISHING_ENABLED",
        "LIVE_DAILY_QUOTA_AVAILABLE",
    }.issubset(set(_codes(result)))

    result_limit = _gate(
        db,
        "LIVE",
        settings=_settings(
            publishing_enabled=True,
            buffer_publishing_enabled=True,
            routine_pinterest_daily_write_limit=2,
        ),
    )
    assert "DAILY_WRITE_LIMIT_ONE" in _codes(result_limit)
    db.close()


def test_dry_run_ignores_exhausted_quota_and_live_publishing_flags(monkeypatch):
    db = _db()
    monkeypatch.setattr(activation, "daily_provider_write_count", lambda *args, **kwargs: 99)
    result = _gate(
        db,
        "DRY_RUN",
        settings=_settings(
            publishing_enabled=False,
            buffer_publishing_enabled=False,
        ),
    )
    assert result["ready"] is True
    assert "LIVE_DAILY_QUOTA_AVAILABLE" not in [item["code"] for item in result["checks"]]
    assert "LIVE_PUBLISHING_ENABLED" not in [item["code"] for item in result["checks"]]
    db.close()


def test_check_order_and_fingerprint_are_deterministic_and_candidate_bound():
    db = _db()
    first = _gate(db)
    second = _gate(db)
    assert [item["code"] for item in first["checks"]] == [item["code"] for item in second["checks"]]
    assert first["activation_fingerprint"] == second["activation_fingerprint"]

    changed = _gate(
        db,
        candidate_snapshot=_candidates(eligible_publication_ids=["pub-2"]),
    )
    assert changed["activation_fingerprint"] != first["activation_fingerprint"]
    db.close()


def test_gate_is_database_read_only(monkeypatch):
    db = _db()
    control = db.get(RoutinePublishingControl, "default")
    before = (control.state, control.pause_reason, len(db.new), len(db.dirty), len(db.deleted))

    monkeypatch.setattr(activation, "daily_provider_write_count", lambda *args, **kwargs: 0)
    result = _gate(db)
    after_control = db.get(RoutinePublishingControl, "default")
    after = (
        after_control.state,
        after_control.pause_reason,
        len(db.new),
        len(db.dirty),
        len(db.deleted),
    )

    assert result["state_mutated"] is False
    assert result["provider_called"] is False
    assert before == after
    db.close()


def test_candidate_snapshot_uses_existing_permit_validator_and_persisted_routing(monkeypatch):
    db = _db()
    connection = PinterestConnection(
        id="conn-1",
        provider="pinterest",
        external_user_id="user-1",
        access_token_ciphertext="cipher-a",
        refresh_token_ciphertext="cipher-r",
        status="CONNECTED",
        boards_last_synced_at=NOW,
    )
    board = PinterestBoard(
        id="board-1",
        connection_id="conn-1",
        external_board_id="ext-board-1",
        name="Board",
        is_active=True,
        is_eligible=True,
        last_synced_at=NOW,
    )
    publication = PinPublication(
        id="pub-route",
        draft_id="draft-1",
        creative_id="creative-1",
        approval_id="approval-1",
        pinterest_connection_id="conn-1",
        pinterest_board_record_id="board-1",
        pinterest_board_id_snapshot="ext-board-1",
        publication_fingerprint="a" * 64,
        status=PublicationStatus.SCHEDULED,
        scheduled_for=NOW,
    )
    db.add_all([connection, board, publication])
    db.commit()

    permit = SimpleNamespace(id="permit-1", status="ACTIVE")
    monkeypatch.setattr(activation, "active_permit", lambda _db, _id: permit)
    monkeypatch.setattr(
        activation,
        "validate_permit",
        lambda *args, **kwargs: {"valid": True, "status": "ACTIVE"},
    )

    snapshot = activation.routine_activation_candidate_snapshot(db, now=NOW)
    assert snapshot["due_count"] == 1
    assert snapshot["active_permit_count"] == 1
    assert snapshot["valid_permit_count"] == 1
    assert snapshot["routing_current_count"] == 1
    assert snapshot["eligible_publication_ids"] == ["pub-route"]

    board.last_synced_at = datetime(2026, 9, 19, 18, 0, tzinfo=timezone.utc)
    db.commit()
    snapshot = activation.routine_activation_candidate_snapshot(db, now=NOW)
    assert snapshot["eligible_count"] == 0
    assert snapshot["invalid_reasons"] == {"PERSISTED_PINTEREST_ROUTING_STALE": 1}
    db.close()
