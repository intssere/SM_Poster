"""Isolated safety tests for the one-shot routine scheduler canary."""
from __future__ import annotations

import asyncio
import time
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core import auth
from app.core.config import get_settings
from app.models.domain import (
    PinPublication,
    PinterestPortfolioPlanItem,
    PublicationAttempt,
    PublicationStatus,
)
from app.models.routine_publishing import (
    RoutineAttemptBoundary,
    RoutineDispatchPermit,
    RoutinePublishingControl,
    RoutinePublishingRun,
    RoutineScheduledQuotaReservation,
)
from app.services import routine_pinterest_scheduler as scheduler
from app.services import routine_pinterest_worker as worker
from app.services import routine_scheduler_canary as one_shot_canary
from app.services.publication_scheduler import request_fingerprint_for
from app.services.routine_scheduler_canary_context import (
    CanarySafetyError,
    RoutineSchedulerCanaryContext,
    validate_canary_context,
)
from test_scheduled_autonomous_readiness import (
    NOW,
    _database,
    _seed_positive_ready_autonomous_chain,
    _settings,
)


class Lease:
    supported = True
    last_status = "ACQUIRED"
    held = True

    def __init__(self, *, valid=True):
        self.valid = valid

    def validate(self):
        return self.valid


class ProviderTripwire:
    def __getattr__(self, name):
        raise AssertionError(f"provider access forbidden for canary: {name}")


async def _run_one_shot_canary(*, settings):
    return await one_shot_canary.run_routine_scheduler_canary(settings=settings)


def _closed_settings(**overrides):
    values = dict(
        routine_scheduler_canary_enabled=True,
        routine_pinterest_scheduler_enabled=False,
        routine_pinterest_worker_enabled=False,
        routine_scheduled_autonomy_enabled=False,
        routine_pinterest_dry_run=True,
        routine_pinterest_batch_size=1,
        routine_pinterest_daily_write_limit=1,
        publishing_enabled=False,
        buffer_publishing_enabled=False,
        routine_buffer_dispatch_enabled=False,
        routine_scheduled_live_admission_enabled=False,
        routine_autonomous_authorization_enabled=False,
        pinterest_write_scope_enabled=False,
        pinterest_board_write_scope_enabled=False,
        pinterest_board_provisioning_enabled=False,
        pinterest_autonomous_board_ensure_enabled=False,
        pinterest_seo_brief_persistence_enabled=False,
        pinterest_autonomous_generation_enabled=False,
        pinterest_autonomous_execution_enabled=False,
        pinterest_portfolio_planner_enabled=False,
        pinterest_optimizer_enabled=False,
        pinterest_optimizer_apply_enabled=False,
        pinterest_portfolio_activation_enabled=False,
        pinterest_analytics_ingestion_enabled=False,
        pinterest_learning_snapshot_persistence_enabled=False,
        buffer_single_pin_pilot_enabled=False,
        pinterest_single_pin_pilot_enabled=False,
    )
    values.update(overrides)
    return _settings(**values)


def _context(publication, permit, *, lease=None, deadline=None, **overrides):
    values = dict(
        lease=lease or Lease(),
        idempotency_key="one-shot-canary-key",
        target_publication_id=publication.id,
        target_permit_id=permit.id,
        expected_publication_fingerprint=publication.publication_fingerprint,
        expected_request_fingerprint=request_fingerprint_for(publication),
        expected_route_id=publication.pinterest_board_record_id,
        deadline_monotonic=deadline if deadline is not None else time.monotonic() + 60,
    )
    values.update(overrides)
    return RoutineSchedulerCanaryContext(**values)


@pytest.fixture
def seeded_db():
    engine, sessions = _database()
    db = sessions()
    item, publication, permit = _seed_positive_ready_autonomous_chain(db)
    control = db.get(RoutinePublishingControl, "default")
    control.state = "PAUSED"
    db.commit()
    yield db, item, publication, permit
    db.close()
    engine.dispose()


def _durable_snapshot(db, publication_id):
    db.expire_all()
    publication = db.get(PinPublication, publication_id)
    permit = db.scalar(select(RoutineDispatchPermit).where(
        RoutineDispatchPermit.publication_id == publication_id
    ))
    return (
        publication.status,
        permit.status,
        permit.consumed_at,
        db.scalars(select(PublicationAttempt)).all(),
        db.scalars(select(RoutineAttemptBoundary)).all(),
        db.scalars(select(RoutineScheduledQuotaReservation)).all(),
        db.get(RoutinePublishingControl, "default").state,
    )


def test_canary_context_is_default_off_and_rejects_open_gates(seeded_db):
    _, _, publication, permit = seeded_db
    context = _context(publication, permit)
    with pytest.raises(CanarySafetyError, match="CANARY_GATE_NOT_ENABLED"):
        validate_canary_context(context, _closed_settings(routine_scheduler_canary_enabled=False))

    for key in (
        "routine_pinterest_scheduler_enabled",
        "routine_pinterest_worker_enabled",
        "routine_scheduled_autonomy_enabled",
        "publishing_enabled",
        "buffer_publishing_enabled",
        "routine_buffer_dispatch_enabled",
        "routine_scheduled_live_admission_enabled",
        "routine_autonomous_authorization_enabled",
        "pinterest_write_scope_enabled",
        "pinterest_board_write_scope_enabled",
        "pinterest_board_provisioning_enabled",
        "pinterest_autonomous_board_ensure_enabled",
        "pinterest_seo_brief_persistence_enabled",
        "pinterest_autonomous_generation_enabled",
        "pinterest_autonomous_execution_enabled",
        "pinterest_portfolio_planner_enabled",
        "pinterest_optimizer_enabled",
        "pinterest_optimizer_apply_enabled",
        "pinterest_portfolio_activation_enabled",
        "pinterest_analytics_ingestion_enabled",
        "pinterest_learning_snapshot_persistence_enabled",
        "buffer_single_pin_pilot_enabled",
        "pinterest_single_pin_pilot_enabled",
    ):
        with pytest.raises(CanarySafetyError, match="CANARY_SETTINGS_NOT_CLOSED"):
            validate_canary_context(context, _closed_settings(**{key: True}))


@pytest.mark.asyncio
async def test_one_shot_canary_default_gate_off_does_not_acquire_lease(monkeypatch):
    acquired = []

    class NeverAcquire:
        last_status = "NOT_ATTEMPTED"
        last_error = None

        def __init__(self, _engine):
            pass

        def acquire(self):
            acquired.append(True)
            return "ACQUIRED"

        def release(self):
            pass

    monkeypatch.setattr(one_shot_canary, "PostgresSchedulerLeaderLease", NeverAcquire)
    result = await _run_one_shot_canary(
        settings=_closed_settings(routine_scheduler_canary_enabled=False)
    )
    assert result["status"] == "BLOCKED"
    assert result["code"] == "CANARY_GATE_DISABLED"
    assert result["external_calls"] == 0
    assert acquired == []


@pytest.mark.asyncio
async def test_one_shot_tick_timeout_returns_closed_result(monkeypatch):
    class FakeConnection:
        def detach(self):
            pass

        def execute(self, *args, **kwargs):
            return self

        def commit(self):
            pass

        def close(self):
            pass

    class FakeEngine:
        dialect = type("Dialect", (), {"name": "postgresql"})()

        def connect(self):
            return FakeConnection()

    class FakeSession:
        def rollback(self):
            pass

        def close(self):
            pass

    class AcquiredLease:
        held = True
        backend_pid = 9876
        last_status = "ACQUIRED"
        last_error = None
        released = False

        def acquire(self):
            return "ACQUIRED"

        def validate(self):
            return self.held

        def release(self):
            self.held = False
            self.released = True
            self.last_status = "RELEASED"

    lease = AcquiredLease()
    calls = []

    async def hanging_tick(**kwargs):
        calls.append(kwargs)
        await asyncio.sleep(5)

    monkeypatch.setattr(one_shot_canary, "engine", FakeEngine())
    monkeypatch.setattr(one_shot_canary, "PostgresSchedulerLeaderLease", lambda _engine: lease)
    monkeypatch.setattr(one_shot_canary, "_schema_is_exact_0031", lambda *_: True)
    monkeypatch.setattr(one_shot_canary, "_release_matches_pins", lambda _target: True)
    monkeypatch.setattr(
        one_shot_canary, "_dedicated_session",
        lambda _timeout: (FakeConnection(), FakeSession()),
    )
    monkeypatch.setattr(
        one_shot_canary, "_preflight",
        lambda *args, **kwargs: {
            "counts": {}, "publication": {}, "permit": {}, "control": {},
        },
    )
    monkeypatch.setattr(one_shot_canary, "scheduler_tick", hanging_tick)
    settings = _closed_settings(
        routine_scheduler_canary_timeout_seconds=1,
        routine_scheduler_canary_publication_id="pub",
        routine_scheduler_canary_permit_id="permit",
        routine_scheduler_canary_publication_fingerprint="a" * 64,
        routine_scheduler_canary_request_fingerprint="b" * 64,
        routine_scheduler_canary_route_id="route",
        routine_scheduler_canary_release_commit_sha="c" * 40,
        routine_scheduler_canary_release_tree_sha="d" * 40,
    )

    result = await _run_one_shot_canary(settings=settings)

    assert result["status"] == "BLOCKED"
    assert result["code"] == "CANARY_TICK_TIMEOUT"
    assert result["external_calls"] == 0
    assert len(calls) == 1
    assert lease.released is True


def test_one_shot_route_requires_auth_origin_and_exact_confirmation(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("APP_SECRET_KEY", "s" * 48)
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", auth.hash_password("canary-test-password"))
    monkeypatch.setenv("AUTH_ALLOWED_ORIGINS", "http://localhost:5000")
    monkeypatch.delenv("REPLIT_DEPLOYMENT", raising=False)
    get_settings.cache_clear()

    from app.main import app

    from app.services import routine_scheduler_canary as canary_service

    calls = []

    async def fake_run(*, settings):
        calls.append(settings)
        return {"status": "PASS", "code": "CANARY_ADMISSION_EXERCISED_PROVIDER_FREE"}

    monkeypatch.setattr(canary_service, "run_routine_scheduler_canary", fake_run)
    client = TestClient(app)
    path = "/api/routine-publishing/scheduler-canary/run-once"
    headers = {"Origin": "http://localhost:5000"}
    payload = {
        "confirmed": True,
        "confirmation_text_version": canary_service.CONFIRMATION_TEXT_VERSION,
    }
    try:
        assert client.post(path, json=payload, headers=headers).status_code == 401
        assert client.post(path, json=payload).status_code == 403
        assert client.post(
            path, json=payload, headers={"Origin": "https://not-allowed.example"}
        ).status_code == 403
        client.cookies.set(auth.SESSION_COOKIE, auth.make_session("admin"))

        assert client.post(path, json={
            "confirmed": False,
            "confirmation_text_version": canary_service.CONFIRMATION_TEXT_VERSION,
        }, headers=headers).status_code == 422
        assert client.post(path, json={
            "confirmed": True,
            "confirmation_text_version": "WRONG",
        }, headers=headers).status_code == 422
        assert client.post(path, json={**payload, "unexpected": True}, headers=headers).status_code == 422

        response = client.post(path, json=payload, headers=headers)
        assert response.status_code == 200
        assert response.json() == {
            "status": "PASS",
            "code": "CANARY_ADMISSION_EXERCISED_PROVIDER_FREE",
        }
        assert len(calls) == 1
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize(
    ("lease", "deadline", "message"),
    [
        (Lease(), 0, "CANARY_CONTEXT_EXPIRED"),
        (Lease(valid=False), None, "CANARY_LEASE_INVALID"),
        (type("Standby", (), {"held": False, "validate": lambda self: False})(), None,
         "CANARY_LEASE_NOT_HELD"),
    ],
)
def test_timeout_lost_lock_and_standby_fail_before_opening_worker_session(
    seeded_db, lease, deadline, message
):
    _, _, publication, permit = seeded_db
    context = _context(
        publication, permit, lease=lease,
        deadline=deadline if deadline is not None else time.monotonic() + 60,
    )
    opened = []

    async def runner(*args, **kwargs):
        pytest.fail("worker must not run without a live canary lease")

    with pytest.raises(CanarySafetyError, match=message):
        asyncio.run(scheduler.scheduler_tick(
            settings=_closed_settings(),
            session_factory=lambda: opened.append(True),
            runner=runner,
            canary_context=context,
        ))
    assert opened == []


@pytest.mark.parametrize(
    "case",
    [
        "absent",
        "duplicate",
        "permit",
        "fingerprint",
        "request_fingerprint",
        "permit_fingerprint",
    ],
)
def test_candidate_and_permit_binding_fail_closed_without_a_run(seeded_db, case):
    db, _, publication, permit = seeded_db
    context = _context(publication, permit)
    if case == "absent":
        publication.scheduled_for = NOW + timedelta(minutes=5)
    elif case == "duplicate":
        fields = {
            column.name: getattr(publication, column.name)
            for column in PinPublication.__table__.columns
        }
        fields.update(id="duplicate-canary-publication", publication_fingerprint="f" * 64)
        db.add(PinPublication(**fields))
        db.flush()
    elif case == "permit":
        context = _context(publication, permit, target_permit_id="not-the-permit")
    elif case == "request_fingerprint":
        context = _context(publication, permit, expected_request_fingerprint="1" * 64)
    elif case == "permit_fingerprint":
        permit.request_fingerprint = "1" * 64
    else:
        context = _context(publication, permit, expected_publication_fingerprint="0" * 64)
    db.commit()
    before = _durable_snapshot(db, publication.id)
    runs_before = db.scalars(select(RoutinePublishingRun)).all()

    result = asyncio.run(worker.run_once(
        db, settings=_closed_settings(), now=NOW, canary_context=context,
    ))

    assert result["status"] == "NOT_EXERCISED"
    assert result["dispatched"] == 0
    assert _durable_snapshot(db, publication.id) == before
    assert db.scalars(select(RoutinePublishingRun)).all() == runs_before


def test_paused_control_and_durable_boundaries_remain_unchanged(seeded_db):
    db, _, publication, permit = seeded_db
    assert db.get(RoutinePublishingControl, "default").state == "PAUSED"
    context = _context(publication, permit)
    baseline = _durable_snapshot(db, publication.id)
    result = asyncio.run(worker.run_once(
        db, settings=_closed_settings(), now=NOW, canary_context=context,
    ))

    assert result["status"] == "SUCCEEDED"
    assert result["mode"] == "DRY_RUN"
    assert result["dispatched"] == result["claimed"] == 0
    assert result["canary_evidence"]["provider_calls"] == 0
    assert result["canary_evidence"]["claim_committed"] is False
    assert result["canary_evidence"]["reservation_committed"] is False
    assert _durable_snapshot(db, publication.id) == baseline
    assert db.scalars(select(RoutinePublishingRun)).all()[-1].metadata_json["scheduler_canary"] == {
        "key": context.idempotency_key,
        "target_publication_id": publication.id,
    }


def test_crashed_running_run_blocks_idempotently_without_stale_recovery(seeded_db):
    db, _, publication, permit = seeded_db
    context = _context(publication, permit)
    running = RoutinePublishingRun(
        id="crashed-canary-run",
        mode="DRY_RUN",
        status="RUNNING",
        started_at=NOW - timedelta(days=1),
        heartbeat_at=NOW - timedelta(days=1),
    )
    db.add(running)
    db.commit()
    before = _durable_snapshot(db, publication.id)

    result = asyncio.run(worker.run_once(
        db, settings=_closed_settings(), now=NOW, canary_context=context,
    ))

    assert result["status"] == "NOT_EXERCISED"
    assert result["reason"] == "ROUTINE_WORKER_ALREADY_RUNNING"
    assert _durable_snapshot(db, publication.id) == before
    db.refresh(running)
    assert running.status == "RUNNING"
    assert running.error_code is None
    assert db.scalars(select(RoutinePublishingRun)).all() == [running]


@pytest.mark.parametrize(
    ("drift", "expected_error"),
    [
        ("lease", "CANARY_LEASE_INVALID"),
        ("provider_gate", "CANARY_SETTINGS_NOT_CLOSED"),
    ],
)
def test_admission_rechecks_lease_and_provider_gates_before_commit(
    seeded_db, monkeypatch, drift, expected_error,
):
    db, item, publication, permit = seeded_db
    lease = Lease()
    context = _context(publication, permit, lease=lease)
    monkeypatch.setattr(
        worker,
        "scheduled_autonomous_readiness",
        lambda *args, **kwargs: {
            "ready": True,
            "blockers": [],
            "checks": [
                {"code": "PERSISTED_BOARD_ROUTE_UNAMBIGUOUS", "passed": True},
                {"code": "CANARY_TARGET_BINDING_CURRENT", "passed": True},
            ],
            "certificate_fingerprint": "e" * 64,
        },
    )

    admissions = []

    def lose_lease_during_admission(
        db_, *, publication_id, plan_item_id, settings, now,
    ):
        admissions.append((publication_id, plan_item_id))
        current = db_.get(PinPublication, publication_id)
        current.status = PublicationStatus.PUBLISHING
        plan_item = db_.get(PinterestPortfolioPlanItem, plan_item_id)
        db_.add(RoutineScheduledQuotaReservation(
            publication_id=publication_id,
            plan_id=plan_item.plan_id,
            plan_item_id=plan_item.id,
            product_id=plan_item.product_id,
            vendor_key="diamond shelf",
            board_id=plan_item.local_board_id,
            scheduled_for=plan_item.planned_date,
            month_start=date(2026, 10, 1),
        ))
        db_.flush()
        if drift == "lease":
            lease.valid = False
        else:
            object.__setattr__(settings, "publishing_enabled", True)

    monkeypatch.setattr(worker, "admit_scheduled_publication", lose_lease_during_admission)
    before = _durable_snapshot(db, publication.id)

    with pytest.raises(CanarySafetyError, match=expected_error):
        asyncio.run(worker.run_once(
            db, settings=_closed_settings(), now=NOW, canary_context=context,
        ))

    assert admissions == [(publication.id, item.id)]
    assert _durable_snapshot(db, publication.id) == before
    assert db.scalars(select(RoutineScheduledQuotaReservation)).all() == []
    assert db.scalars(select(PublicationAttempt)).all() == []
    assert db.scalars(select(RoutineAttemptBoundary)).all() == []
    runs = db.scalars(select(RoutinePublishingRun)).all()
    assert len(runs) == 1 and runs[0].status == "RUNNING"


@pytest.mark.parametrize("provider_arg", ["gateway", "media_client", "resolver"])
def test_provider_handles_are_rejected_before_canary_work(seeded_db, provider_arg):
    db, _, publication, permit = seeded_db
    context = _context(publication, permit)
    before = _durable_snapshot(db, publication.id)
    with pytest.raises(CanarySafetyError, match="CANARY_PROVIDER_OR_TARGETED_LIVE_PATH_FORBIDDEN"):
        asyncio.run(worker.run_once(
            db, settings=_closed_settings(), now=NOW, canary_context=context,
            **{provider_arg: ProviderTripwire()},
        ))
    assert _durable_snapshot(db, publication.id) == before
    assert db.scalars(select(RoutinePublishingRun)).all() == []