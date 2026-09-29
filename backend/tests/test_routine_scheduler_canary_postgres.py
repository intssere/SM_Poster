"""PostgreSQL proof that the scheduled canary is observational only."""
from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models.domain import PinPublication, PublicationAttempt, PublicationStatus
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
from app.services.routine_scheduler_lease import PostgresSchedulerLeaderLease
from app.services.routine_scheduler_canary_context import (
    CanarySafetyError,
    RoutineSchedulerCanaryContext,
)
from test_routine_scheduled_commitments_postgres import local_postgres_url
from test_scheduled_autonomous_readiness import (
    NOW,
    _seed_positive_ready_autonomous_chain,
    _settings,
)


async def _run_one_shot_canary(settings):
    return await one_shot_canary.run_routine_scheduler_canary(settings=settings)


def _canary_settings(publication, permit, **overrides):
    values = {
        "routine_scheduler_canary_enabled": True,
        "routine_pinterest_scheduler_enabled": False,
        "routine_pinterest_worker_enabled": False,
        "routine_scheduled_autonomy_enabled": False,
        "routine_scheduler_canary_publication_id": publication.id,
        "routine_scheduler_canary_permit_id": permit.id,
        "routine_scheduler_canary_publication_fingerprint": publication.publication_fingerprint,
        "routine_scheduler_canary_request_fingerprint": request_fingerprint_for(publication),
        "routine_scheduler_canary_route_id": publication.pinterest_board_record_id,
        "routine_scheduler_canary_release_commit_sha": "a" * 40,
        "routine_scheduler_canary_release_tree_sha": "b" * 40,
        "routine_scheduler_canary_timeout_seconds": 5,
    }
    values.update(overrides)
    return _settings(**values)


def _postgres_rows(db, model):
    columns = list(model.__table__.columns)
    statement = select(model)
    for primary_key in model.__table__.primary_key.columns:
        statement = statement.order_by(primary_key)
    return tuple(
        tuple(getattr(row, column.key) for column in columns)
        for row in db.scalars(statement).all()
    )


def _postgres_canary_snapshot(db):
    db.expire_all()
    models = (
        PinPublication,
        RoutineDispatchPermit,
        PublicationAttempt,
        RoutineAttemptBoundary,
        RoutineScheduledQuotaReservation,
        RoutinePublishingControl,
        RoutinePublishingRun,
    )
    return tuple(_postgres_rows(db, model) for model in models)


class FakeCanaryLease:
    def __init__(self):
        self.supported = True
        self.held = False
        self.last_status = "NOT_ATTEMPTED"
        self.last_error = None
        self.backend_pid = 24680
        self.acquire_calls = 0

    def acquire(self):
        self.acquire_calls += 1
        self.held = True
        self.last_status = "ACQUIRED"
        return "ACQUIRED"

    def validate(self):
        return self.held

    def release(self):
        self.held = False
        self.last_status = "RELEASED"


def _patch_canary_service(monkeypatch, engine, lease):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is not None else NOW.replace(tzinfo=None)

    monkeypatch.setattr(one_shot_canary, "engine", engine)
    monkeypatch.setattr(one_shot_canary, "datetime", FrozenDateTime)
    monkeypatch.setattr(one_shot_canary, "_schema_is_exact_0031", lambda *_args: True)
    monkeypatch.setattr(one_shot_canary, "_release_matches_pins", lambda _target: True)
    monkeypatch.setattr(
        one_shot_canary, "PostgresSchedulerLeaderLease", lambda _engine: lease,
    )
    monkeypatch.setattr(
        one_shot_canary, "scheduler_status",
        lambda _settings: {
            "enabled": False, "started": False, "task_running": False,
            "tick_running": False, "lease_supported": True, "lease_held": False,
        },
    )
    monkeypatch.setattr(
        one_shot_canary, "routine_readiness_snapshot",
        lambda *args, **kwargs: {"alerts": []},
    )


@pytest.fixture
def postgres_sessions(local_postgres_url):
    engine = create_engine(local_postgres_url)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    yield engine, sessions
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.mark.asyncio
async def test_positive_canary_runs_real_postgres_dry_run_without_durable_admission(
    postgres_sessions, monkeypatch,
):
    engine, sessions = postgres_sessions
    scheduler.reset_scheduler_state_for_tests()
    try:
        with sessions() as seed:
            _, publication, permit = _seed_positive_ready_autonomous_chain(seed)
            control = seed.get(RoutinePublishingControl, "default")
            control.state = "PAUSED"
            seed.commit()

        settings = _settings(
            routine_pinterest_scheduler_enabled=False,
            routine_pinterest_worker_enabled=False,
            routine_scheduler_canary_enabled=True,
            routine_scheduled_autonomy_enabled=False,
            routine_scheduler_canary_publication_id=publication.id,
            routine_scheduler_canary_permit_id=permit.id,
            routine_scheduler_canary_publication_fingerprint=publication.publication_fingerprint,
            routine_scheduler_canary_request_fingerprint=request_fingerprint_for(publication),
            routine_scheduler_canary_route_id=publication.pinterest_board_record_id,
            routine_scheduler_canary_release_commit_sha="a" * 40,
            routine_scheduler_canary_release_tree_sha="b" * 40,
        )

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW if tz is not None else NOW.replace(tzinfo=None)

        class AcquiredLease:
            supported = True
            held = True
            last_status = "ACQUIRED"
            last_error = None
            backend_pid = 24680

            def acquire(self):
                return "ACQUIRED"

            def validate(self):
                return True

            def release(self):
                self.held = False
                self.last_status = "RELEASED"

        monkeypatch.setattr(one_shot_canary, "engine", engine)
        monkeypatch.setattr(one_shot_canary, "datetime", FrozenDateTime)
        monkeypatch.setattr(one_shot_canary, "_schema_is_exact_0031", lambda *_: True)
        monkeypatch.setattr(one_shot_canary, "_release_matches_pins", lambda _target: True)
        monkeypatch.setattr(one_shot_canary, "PostgresSchedulerLeaderLease", lambda _engine: AcquiredLease())
        monkeypatch.setattr(
            one_shot_canary, "scheduler_status",
            lambda _settings: {
                "enabled": False, "started": False, "task_running": False,
                "tick_running": False, "lease_supported": True, "lease_held": False,
            },
        )
        monkeypatch.setattr(
            one_shot_canary, "routine_readiness_snapshot",
            lambda *args, **kwargs: {"alerts": []},
        )
        monkeypatch.setattr(worker, "datetime", FrozenDateTime)

        result = await _run_one_shot_canary(settings)

        assert result["status"] == "PASS"
        assert result["code"] == "CANARY_ADMISSION_EXERCISED_PROVIDER_FREE"
        assert result["external_calls"] == 0
        assert result["publication_fingerprint"] == publication.publication_fingerprint
        assert result["request_fingerprint"] == request_fingerprint_for(publication)
        assert result["route_fingerprint"] == hashlib.sha256(
            publication.pinterest_board_record_id.encode("utf-8")
        ).hexdigest()
        assert result["counts"] == {
            "scanned": 1, "eligible": 1, "claimed": 0, "dispatched": 0,
            "published": 0, "failed": 0, "unknown": 0,
        }
        assert result["certificate_quota_outcomes"] == [{
            "ready": True,
            "offline_validated": True,
            "blockers": [],
            "atomic_admission_evaluated": True,
            "would_admit": True,
            "quota_headroom": {
                "daily": 0,
                "monthly": 0,
                "product": 2,
                "vendor": 0,
                "board": 0,
            },
            "already_committed": True,
            "already_reserved": False,
            "claim_committed": False,
            "reservation_committed": False,
            "external_requests": 0,
        }]
        with sessions() as check:
            assert check.get(RoutinePublishingControl, "default").state == "PAUSED"
            assert check.get(PinPublication, publication.id).status == PublicationStatus.SCHEDULED
            assert check.get(RoutineDispatchPermit, permit.id).status == "ACTIVE"
            assert check.scalars(select(RoutineScheduledQuotaReservation)).all() == []
            assert check.scalars(select(PublicationAttempt)).all() == []
            assert check.scalars(select(RoutineAttemptBoundary)).all() == []
            runs = check.scalars(select(RoutinePublishingRun)).all()
            assert len(runs) == 1
            assert runs[0].status == "SUCCEEDED"
            assert runs[0].metadata_json["scheduler_canary"]["key"]
            certificate = runs[0].metadata_json["scheduled_autonomy_certificates"][0]
            assert certificate["ready"] is True
            assert certificate["offline_validated"] is True
            assert certificate["external_requests"] == 0
        assert result["lease"]["release_succeeded"] is True
        assert scheduler._state["lease_role"] == "stopped"
        assert scheduler._state["lease_held"] is False
        assert scheduler._state["last_lease_status"] == "RELEASED"
        assert scheduler.scheduler_status(settings)["lease_held"] is False

        with sessions() as check:
            before_replay = _postgres_canary_snapshot(check)
        replay = await _run_one_shot_canary(settings)
        assert replay["status"] == "BLOCKED"
        assert replay["code"] == "CANARY_IDEMPOTENCY_REQUIRES_RECONCILIATION"
        with sessions() as check:
            assert _postgres_canary_snapshot(check) == before_replay
    finally:
        scheduler.reset_scheduler_state_for_tests()


@pytest.mark.parametrize(
    ("case", "expected_status", "expected_code"),
    [
        ("gate_disabled", "BLOCKED", "CANARY_GATE_DISABLED"),
        ("absent_candidate", "NOT_EXERCISED", "CANARY_DUE_PUBLICATION_IDENTITY_OR_CARDINALITY"),
        ("absent_permit", "NOT_EXERCISED", "CANARY_ACTIVE_PERMIT_IDENTITY_OR_CARDINALITY"),
        ("duplicate_candidate", "NOT_EXERCISED", "CANARY_DUE_PUBLICATION_IDENTITY_OR_CARDINALITY"),
        ("publication_fingerprint", "NOT_EXERCISED", "CANARY_FIXED_TARGET_BINDING_MISMATCH"),
        ("request_fingerprint", "NOT_EXERCISED", "CANARY_FIXED_TARGET_BINDING_MISMATCH"),
    ],
)
@pytest.mark.asyncio
async def test_postgres_preflight_rejections_leave_durable_rows_unchanged(
    postgres_sessions, monkeypatch, case, expected_status, expected_code,
):
    engine, sessions = postgres_sessions
    with sessions() as seed:
        _, publication, permit = _seed_positive_ready_autonomous_chain(seed)
        seed.get(RoutinePublishingControl, "default").state = "PAUSED"
        if case == "absent_candidate":
            publication.scheduled_for = NOW + timedelta(days=1)
        elif case == "absent_permit":
            permit.status = "REVOKED"
        elif case == "duplicate_candidate":
            fields = {
                column.name: getattr(publication, column.name)
                for column in PinPublication.__table__.columns
            }
            fields.update(
                id="duplicate-canary-publication",
                publication_fingerprint="f" * 64,
            )
            seed.add(PinPublication(**fields))
        seed.commit()

    overrides = {}
    if case == "gate_disabled":
        overrides["routine_scheduler_canary_enabled"] = False
    elif case == "publication_fingerprint":
        overrides["routine_scheduler_canary_publication_fingerprint"] = "0" * 64
    elif case == "request_fingerprint":
        overrides["routine_scheduler_canary_request_fingerprint"] = "1" * 64
    settings = _canary_settings(publication, permit, **overrides)
    lease = FakeCanaryLease()
    _patch_canary_service(monkeypatch, engine, lease)
    with sessions() as check:
        before = _postgres_canary_snapshot(check)
        assert check.scalars(select(RoutinePublishingRun)).all() == []

    result = await _run_one_shot_canary(settings)

    assert (result["status"], result["code"]) == (expected_status, expected_code), result
    assert result["external_calls"] == 0
    assert lease.acquire_calls == (0 if case == "gate_disabled" else 1)
    with sessions() as check:
        assert _postgres_canary_snapshot(check) == before
        assert check.scalars(select(RoutinePublishingRun)).all() == []


@pytest.mark.parametrize("provider_arg", ["gateway", "media_client", "resolver"])
def test_postgres_worker_rejects_provider_handles_without_mutation(
    postgres_sessions, provider_arg,
):
    _, sessions = postgres_sessions
    with sessions() as seed:
        _, publication, permit = _seed_positive_ready_autonomous_chain(seed)
        seed.get(RoutinePublishingControl, "default").state = "PAUSED"
        seed.commit()

    class HeldLease:
        held = True

        def validate(self):
            return True

    context = RoutineSchedulerCanaryContext(
        lease=HeldLease(),
        idempotency_key="provider-handle-postgres-canary",
        target_publication_id=publication.id,
        target_permit_id=permit.id,
        expected_publication_fingerprint=publication.publication_fingerprint,
        expected_request_fingerprint=request_fingerprint_for(publication),
        expected_route_id=publication.pinterest_board_record_id,
        deadline_monotonic=time.monotonic() + 60,
    )
    settings = _canary_settings(publication, permit)
    with sessions() as check:
        before = _postgres_canary_snapshot(check)
        assert check.scalars(select(RoutinePublishingRun)).all() == []

    with sessions() as db:
        with pytest.raises(
            CanarySafetyError,
            match="CANARY_PROVIDER_OR_TARGETED_LIVE_PATH_FORBIDDEN",
        ):
            asyncio.run(worker.run_once(
                db,
                settings=settings,
                now=NOW,
                canary_context=context,
                **{provider_arg: object()},
            ))

    with sessions() as check:
        assert _postgres_canary_snapshot(check) == before
        assert check.scalars(select(RoutinePublishingRun)).all() == []


@pytest.mark.asyncio
async def test_postgres_one_shot_timeout_leaves_candidate_and_ledgers_unchanged(
    postgres_sessions, monkeypatch,
):
    engine, sessions = postgres_sessions
    with sessions() as seed:
        _, publication, permit = _seed_positive_ready_autonomous_chain(seed)
        seed.get(RoutinePublishingControl, "default").state = "PAUSED"
        seed.commit()

    settings = _canary_settings(
        publication, permit, routine_scheduler_canary_timeout_seconds=1,
    )
    lease = FakeCanaryLease()
    _patch_canary_service(monkeypatch, engine, lease)
    calls = []

    async def hanging_tick(**kwargs):
        calls.append(kwargs)
        await asyncio.sleep(5)

    monkeypatch.setattr(one_shot_canary, "scheduler_tick", hanging_tick)
    with sessions() as check:
        before = _postgres_canary_snapshot(check)
        assert check.scalars(select(RoutinePublishingRun)).all() == []

    result = await _run_one_shot_canary(settings)

    assert result["status"] == "BLOCKED"
    assert result["code"] == "CANARY_TICK_TIMEOUT"
    assert result["external_calls"] == 0
    assert calls and lease.acquire_calls == 1
    assert lease.held is False
    with sessions() as check:
        assert _postgres_canary_snapshot(check) == before
        assert check.scalars(select(RoutinePublishingRun)).all() == []


@pytest.mark.asyncio
async def test_postgres_preflight_blocks_crashed_running_run_without_recovery(
    postgres_sessions, monkeypatch,
):
    engine, sessions = postgres_sessions
    with sessions() as seed:
        _, publication, permit = _seed_positive_ready_autonomous_chain(seed)
        seed.get(RoutinePublishingControl, "default").state = "PAUSED"
        crashed = RoutinePublishingRun(
            id="crashed-postgres-canary-run",
            mode="DRY_RUN",
            status="RUNNING",
            started_at=NOW - timedelta(days=1),
            heartbeat_at=NOW - timedelta(days=1),
        )
        seed.add(crashed)
        seed.commit()

    settings = _canary_settings(publication, permit)
    lease = FakeCanaryLease()
    _patch_canary_service(monkeypatch, engine, lease)
    with sessions() as check:
        before = _postgres_canary_snapshot(check)

    result = await _run_one_shot_canary(settings)

    assert result["status"] == "BLOCKED"
    assert result["code"] == "ROUTINE_WORKER_ALREADY_RUNNING"
    with sessions() as check:
        assert _postgres_canary_snapshot(check) == before
        only_run = check.scalars(select(RoutinePublishingRun)).all()
        assert len(only_run) == 1
        assert only_run[0].status == "RUNNING"


def test_schema_guard_rejects_metadata_created_schema_at_revision_0031(
    postgres_sessions, monkeypatch,
):
    engine, _ = postgres_sessions
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE public.alembic_version "
            "(version_num varchar(32) NOT NULL PRIMARY KEY)"
        ))
        connection.execute(text(
            "INSERT INTO public.alembic_version (version_num) VALUES ('0031')"
        ))
    monkeypatch.setattr(one_shot_canary, "engine", engine)
    calls = []
    real_verify = one_shot_canary.verify_frozen_schema_at_head

    def verify(connection, *, revision):
        calls.append(revision)
        return real_verify(connection, revision=revision)

    monkeypatch.setattr(one_shot_canary, "verify_frozen_schema_at_head", verify)
    try:
        before_tables = set(inspect(engine).get_table_names(schema="public"))
        with engine.connect() as connection:
            assert connection.scalar(text(
                'SELECT version_num FROM public.alembic_version'
            )) == "0031"

        assert one_shot_canary._schema_is_exact_0031() is False
        assert calls == ["0031"]
        assert set(inspect(engine).get_table_names(schema="public")) == before_tables
        with engine.connect() as connection:
            assert connection.scalar(text(
                'SELECT version_num FROM public.alembic_version'
            )) == "0031"
    finally:
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS public.alembic_version"))


def test_real_postgres_canary_lease_excludes_second_instance_and_fails_over(
    local_postgres_url,
):
    engine = create_engine(local_postgres_url, pool_pre_ping=True)
    leader = PostgresSchedulerLeaderLease(engine)
    follower = PostgresSchedulerLeaderLease(engine)
    try:
        assert leader.acquire() == "ACQUIRED"
        assert leader.held is True
        assert leader.validate() is True
        assert leader.backend_pid is not None

        assert follower.acquire() == "STANDBY"
        assert follower.held is False

        leader.release()
        assert leader.held is False
        assert follower.acquire() == "ACQUIRED"
        assert follower.held is True
        assert follower.validate() is True
    finally:
        leader.release()
        follower.release()
        engine.dispose()


@pytest.mark.parametrize(
    ("drift", "expected_error"),
    [
        ("lease", "CANARY_LEASE_INVALID"),
        ("provider_gate", "CANARY_SETTINGS_NOT_CLOSED"),
    ],
)
def test_lease_or_gate_drift_after_real_pg_admission_rolls_back_preview(
    postgres_sessions, monkeypatch, drift, expected_error,
):
    _, sessions = postgres_sessions

    class Lease:
        held = True
        valid = True

        def validate(self):
            return self.valid

    lease = Lease()
    with sessions() as seed:
        _, publication, permit = _seed_positive_ready_autonomous_chain(seed)
        seed.get(RoutinePublishingControl, "default").state = "PAUSED"
        seed.commit()

    settings = _settings(
        routine_scheduler_canary_enabled=True,
        routine_pinterest_scheduler_enabled=False,
        routine_pinterest_worker_enabled=False,
        routine_scheduled_autonomy_enabled=False,
    )
    context = RoutineSchedulerCanaryContext(
        lease=lease,
        idempotency_key="pg-admission-lease-loss",
        target_publication_id=publication.id,
        target_permit_id=permit.id,
        expected_publication_fingerprint=publication.publication_fingerprint,
        expected_request_fingerprint=request_fingerprint_for(publication),
        expected_route_id=publication.pinterest_board_record_id,
        deadline_monotonic=time.monotonic() + 60,
    )

    real_admit = worker.admit_scheduled_publication

    def admit_then_drift(db, **kwargs):
        result = real_admit(db, **kwargs)
        if drift == "lease":
            lease.valid = False
        else:
            object.__setattr__(settings, "publishing_enabled", True)
        return result

    monkeypatch.setattr(worker, "admit_scheduled_publication", admit_then_drift)
    with sessions() as db:
        with pytest.raises(CanarySafetyError, match=expected_error):
            asyncio.run(worker.run_once(
                db, settings=settings, now=NOW, canary_context=context,
            ))

    with sessions() as check:
        assert check.get(RoutinePublishingControl, "default").state == "PAUSED"
        assert check.get(PinPublication, publication.id).status == PublicationStatus.SCHEDULED
        current_permit = check.get(RoutineDispatchPermit, permit.id)
        assert current_permit.status == "ACTIVE"
        assert current_permit.consumed_at is None
        assert check.scalars(select(RoutineScheduledQuotaReservation)).all() == []
        assert check.scalars(select(PublicationAttempt)).all() == []
        assert check.scalars(select(RoutineAttemptBoundary)).all() == []
        runs = check.scalars(select(RoutinePublishingRun)).all()
        assert len(runs) == 1 and runs[0].status == "RUNNING"