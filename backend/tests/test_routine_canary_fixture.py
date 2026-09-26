from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.models.domain import PinPublication, PublicationStatus
from app.models.routine_publishing import RoutineDispatchPermit, RoutinePublishingControl
from app.services import routine_canary_fixture as fixture


NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def _settings(**overrides):
    values = dict(
        database_url="sqlite+pysqlite:///:memory:",
        publishing_enabled=False,
        buffer_publishing_enabled=False,
        buffer_single_pin_pilot_enabled=False,
        pinterest_single_pin_pilot_enabled=False,
        routine_pinterest_worker_enabled=False,
        routine_buffer_dispatch_enabled=False,
        routine_pinterest_scheduler_enabled=False,
        routine_autonomous_authorization_enabled=False,
        routine_pinterest_dry_run=True,
        routine_pinterest_batch_size=1,
        routine_pinterest_daily_write_limit=1,
        pinterest_autonomous_generation_enabled=False,
        pinterest_autonomous_execution_enabled=False,
        pinterest_autonomous_board_ensure_enabled=False,
        pinterest_write_scope_enabled=False,
        pinterest_board_write_scope_enabled=False,
        pinterest_board_provisioning_enabled=False,
    )
    values.update(overrides)
    return Settings(**values)


def _scheduler(**overrides):
    values = dict(
        enabled=False, started=False, task_running=False, tick_running=False,
        lease_supported=True, lease_held=False,
    )
    values.update(overrides)
    return values


def _db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    db = Session()
    db.add(RoutinePublishingControl(id="default", state="PAUSED", paused_at=NOW, paused_by="test"))
    publication = PinPublication(
        id="canary-source",
        draft_id="draft-1",
        creative_id="creative-1",
        approval_id="approval-1",
        pinterest_connection_id="conn-1",
        pinterest_board_record_id="board-1",
        pinterest_board_id_snapshot="external-board",
        publication_fingerprint="a" * 64,
        text_fingerprint="b" * 64,
        creative_fingerprint="c" * 64,
        status=PublicationStatus.APPROVED,
        scheduled_for=None,
        title_snapshot="Canary",
        description_snapshot="Canary description",
        alt_text_snapshot="Canary alt",
        destination_url="https://diamondshelf.us/products/canary",
        utm_url="https://diamondshelf.us/products/canary?utm_source=pinterest",
        media_url_snapshot="https://cdn.example/canary.jpg",
        source_image_id="source-1",
        template_id="template-1",
        template_key="template",
        template_version=1,
    )
    db.add(publication)
    db.commit()
    return engine, db


def _wire_happy(monkeypatch, db, *, bad_post=False):
    monkeypatch.setattr(fixture, "_persisted_routing_current", lambda *_: True)
    monkeypatch.setattr(
        fixture,
        "routine_readiness_snapshot",
        lambda *a, **k: {"ready": True, "alerts": []},
    )
    calls = {"candidate": 0}

    def candidates(_db, *, now):
        calls["candidate"] += 1
        if calls["candidate"] == 1:
            return {
                "due_count": 0, "active_permit_count": 0, "valid_permit_count": 0,
                "routing_current_count": 0, "eligible_count": 0,
                "eligible_publication_ids": [], "invalid_reasons": {},
            }
        eligible = 0 if bad_post else 1
        return {
            "due_count": 1, "active_permit_count": 1, "valid_permit_count": 1,
            "routing_current_count": 1, "eligible_count": eligible,
            "eligible_publication_ids": ["canary-source"] if eligible else [],
            "invalid_reasons": {} if eligible else {"TEST_DRIFT": 1},
        }

    monkeypatch.setattr(fixture, "routine_activation_candidate_snapshot", candidates)

    def permit(_db, publication, *, actor, now, commit):
        assert commit is False
        row = RoutineDispatchPermit(
            id="permit-1",
            publication_id=publication.id,
            dispatch_provider="buffer",
            approval_id=publication.approval_id,
            pinterest_board_record_id=publication.pinterest_board_record_id,
            publication_fingerprint=publication.publication_fingerprint,
            request_fingerprint="d" * 64,
            scheduled_for_snapshot=now,
            quality_policy_version="pinterest-quality-v1",
            quality_snapshot={"status": "PASS"},
            duplicate_snapshot={"status": "SAFE_TO_CONTINUE"},
            readiness_snapshot={"dispatch_provider": "buffer"},
            authorized_by=actor,
            authorized_at=now,
            expires_at=now + timedelta(hours=24),
            status="ACTIVE",
        )
        _db.add(row)
        _db.flush()
        return row

    monkeypatch.setattr(fixture, "create_permit", permit)
    monkeypatch.setattr(
        fixture,
        "build_routine_offline_evidence",
        lambda *a, **k: SimpleNamespace(external_requests=0),
    )


def test_happy_path_commits_exactly_one_due_candidate_and_permit(monkeypatch):
    engine, db = _db()
    try:
        _wire_happy(monkeypatch, db)
        result = fixture.prepare_atomic_dry_run_canary_fixture(
            db, publication_id="canary-source", actor="admin",
            settings=_settings(), now=NOW, scheduler_snapshot=_scheduler(),
        )
        assert result == {
            "status": "PREPARED",
            "publication_id": "canary-source",
            "control_state": "PAUSED",
            "dry_run": True,
            "external_requests": 0,
            "counts": {
                "due_count": 1, "active_permit_count": 1, "valid_permit_count": 1,
                "routing_current_count": 1, "eligible_count": 1,
            },
            "invalid_reasons": {},
        }
        publication = db.get(PinPublication, "canary-source")
        assert publication.status == PublicationStatus.SCHEDULED
        assert publication.scheduled_for.replace(tzinfo=timezone.utc) == NOW
        permits = db.scalars(select(RoutineDispatchPermit)).all()
        assert len(permits) == 1 and permits[0].status == "ACTIVE"
        assert db.get(RoutinePublishingControl, "default").state == "PAUSED"
    finally:
        db.close(); engine.dispose()


def test_postcondition_failure_rolls_back_schedule_and_permit(monkeypatch):
    engine, db = _db()
    try:
        _wire_happy(monkeypatch, db, bad_post=True)
        with pytest.raises(fixture.RoutineCanaryFixtureError, match="POSTCONDITION"):
            fixture.prepare_atomic_dry_run_canary_fixture(
                db, publication_id="canary-source", actor="admin",
                settings=_settings(), now=NOW, scheduler_snapshot=_scheduler(),
            )
        publication = db.get(PinPublication, "canary-source")
        assert publication.status == PublicationStatus.APPROVED
        assert publication.scheduled_for is None
        assert db.scalars(select(RoutineDispatchPermit)).all() == []
    finally:
        db.close(); engine.dispose()


@pytest.mark.parametrize(
    ("settings", "scheduler", "code"),
    [
        (_settings(publishing_enabled=True), _scheduler(), "UNSAFE_RUNTIME_GATE_STATE"),
        (_settings(routine_pinterest_dry_run=False), _scheduler(), "ROUTINE_DRY_RUN_CONFIG_REQUIRED"),
        (_settings(routine_pinterest_batch_size=2), _scheduler(), "ROUTINE_DRY_RUN_BATCH_SIZE_MUST_BE_ONE"),
        (_settings(routine_pinterest_daily_write_limit=2), _scheduler(), "ROUTINE_DRY_RUN_DAILY_LIMIT_MUST_BE_ONE"),
        (_settings(), _scheduler(task_running=True), "ROUTINE_SCHEDULER_NOT_DORMANT"),
        (_settings(), _scheduler(lease_held=True), "ROUTINE_SCHEDULER_NOT_DORMANT"),
        (_settings(), _scheduler(lease_supported=False), "ROUTINE_DISTRIBUTED_LEASE_UNSUPPORTED"),
    ],
)
def test_unsafe_runtime_states_fail_before_mutation(monkeypatch, settings, scheduler, code):
    engine, db = _db()
    try:
        monkeypatch.setattr(fixture, "routine_readiness_snapshot", lambda *a, **k: {"ready": True, "alerts": []})
        with pytest.raises(fixture.RoutineCanaryFixtureError, match=code):
            fixture.prepare_atomic_dry_run_canary_fixture(
                db, publication_id="canary-source", actor="admin",
                settings=settings, now=NOW, scheduler_snapshot=scheduler,
            )
        assert db.get(PinPublication, "canary-source").status == PublicationStatus.APPROVED
        assert db.scalars(select(RoutineDispatchPermit)).all() == []
    finally:
        db.close(); engine.dispose()


def test_existing_fixture_state_fails_closed_without_mutation(monkeypatch):
    engine, db = _db()
    try:
        db.add(RoutineDispatchPermit(
            id="existing-permit", publication_id="canary-source",
            dispatch_provider="buffer", approval_id="approval-1",
            pinterest_board_record_id="board-1", publication_fingerprint="a" * 64,
            request_fingerprint="d" * 64, scheduled_for_snapshot=NOW,
            quality_policy_version="pinterest-quality-v1",
            quality_snapshot={}, duplicate_snapshot={}, readiness_snapshot={},
            authorized_by="test", authorized_at=NOW,
            expires_at=NOW + timedelta(hours=1), status="ACTIVE",
        ))
        db.commit()
        monkeypatch.setattr(fixture, "routine_readiness_snapshot", lambda *a, **k: {"ready": True, "alerts": []})
        with pytest.raises(fixture.RoutineCanaryFixtureError, match="PRECONDITION_COUNTS_NOT_ZERO"):
            fixture.prepare_atomic_dry_run_canary_fixture(
                db, publication_id="canary-source", actor="admin",
                settings=_settings(), now=NOW, scheduler_snapshot=_scheduler(),
            )
        assert db.get(PinPublication, "canary-source").status == PublicationStatus.APPROVED
        assert len(db.scalars(select(RoutineDispatchPermit)).all()) == 1
    finally:
        db.close(); engine.dispose()


def test_critical_alert_fails_closed(monkeypatch):
    engine, db = _db()
    try:
        monkeypatch.setattr(
            fixture, "routine_readiness_snapshot",
            lambda *a, **k: {
                "ready": False,
                "alerts": [{"code": "PUBLISH_UNKNOWN_PRESENT", "severity": "critical"}],
            },
        )
        with pytest.raises(fixture.RoutineCanaryFixtureError, match="CRITICAL_OPERATIONAL_ALERT_PRESENT"):
            fixture.prepare_atomic_dry_run_canary_fixture(
                db, publication_id="canary-source", actor="admin",
                settings=_settings(), now=NOW, scheduler_snapshot=_scheduler(),
            )
        assert db.get(PinPublication, "canary-source").status == PublicationStatus.APPROVED
    finally:
        db.close(); engine.dispose()
