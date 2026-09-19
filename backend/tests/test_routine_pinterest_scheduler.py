import asyncio
from datetime import datetime, timezone

import pytest

from app.core.config import Settings
from app.services import routine_pinterest_scheduler as scheduler


class DummySession:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeLease:
    def __init__(self, *, acquire_status="ACQUIRED", validate_result=True, supported=True):
        self.acquire_status = acquire_status
        self.validate_result = validate_result
        self.supported = supported
        self.held = False
        self.last_status = "NOT_ATTEMPTED"
        self.last_error = None
        self.acquired_at = None
        self.lost_at = None
        self.released = False
        self.acquire_called = asyncio.Event()

    def acquire(self):
        self.acquire_called.set()
        self.last_status = self.acquire_status
        if self.acquire_status == "ACQUIRED":
            self.held = True
            self.acquired_at = datetime.now(timezone.utc)
        elif self.acquire_status == "UNSUPPORTED_BACKEND":
            self.last_error = "POSTGRESQL_REQUIRED"
        elif self.acquire_status == "ERROR":
            self.last_error = "SyntheticLeaseError"
        return self.acquire_status

    def validate(self):
        if not self.held:
            return False
        if not self.validate_result:
            self.held = False
            self.last_status = "LOST"
            self.last_error = "BACKEND_PID_CHANGED"
            self.lost_at = datetime.now(timezone.utc)
            return False
        return True

    def release(self):
        self.held = False
        self.released = True
        self.last_status = "RELEASED"


class ControlledSleep:
    def __init__(self):
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, seconds):
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        self.release.clear()
        self.entered.clear()


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
    }
    values.update(overrides)
    return Settings(**values)


def test_scheduler_defaults_fail_closed():
    settings = _settings()
    assert settings.routine_pinterest_scheduler_enabled is False
    assert settings.routine_pinterest_scheduler_interval_seconds == 300
    with pytest.raises(Exception):
        _settings(routine_pinterest_scheduler_interval_seconds=59)


@pytest.mark.asyncio
async def test_disabled_scheduler_does_not_open_lease_session_or_worker():
    scheduler.reset_scheduler_state_for_tests()
    called = {"lease": 0, "session": 0, "runner": 0}

    def lease_factory():
        called["lease"] += 1
        return FakeLease()

    def session_factory():
        called["session"] += 1
        return DummySession()

    async def runner(*args, **kwargs):
        called["runner"] += 1
        return {"status": "SHOULD_NOT_RUN"}

    task = await scheduler.start_scheduler(
        settings=_settings(),
        session_factory=session_factory,
        runner=runner,
        lease_factory=lease_factory,
    )
    result = await scheduler.scheduler_tick(
        settings=_settings(),
        session_factory=session_factory,
        runner=runner,
    )

    assert task is None
    assert result == {"status": "SCHEDULER_DISABLED", "dispatched": 0}
    assert called == {"lease": 0, "session": 0, "runner": 0}
    status = scheduler.scheduler_status(_settings())
    assert status["enabled"] is False
    assert status["started"] is False
    assert status["task_running"] is False
    assert status["lease_role"] == "disabled"
    assert status["lease_held"] is False


@pytest.mark.asyncio
async def test_enabled_direct_tick_requires_distributed_lease():
    scheduler.reset_scheduler_state_for_tests()
    called = {"session": 0, "runner": 0}

    def session_factory():
        called["session"] += 1
        return DummySession()

    async def runner(*args, **kwargs):
        called["runner"] += 1
        return {"status": "SHOULD_NOT_RUN"}

    result = await scheduler.scheduler_tick(
        settings=_settings(routine_pinterest_scheduler_enabled=True),
        session_factory=session_factory,
        runner=runner,
    )
    assert result == {"status": "SCHEDULER_LEASE_NOT_HELD", "dispatched": 0}
    assert called == {"session": 0, "runner": 0}


@pytest.mark.asyncio
async def test_enabled_leader_tick_reuses_worker_and_closes_session():
    scheduler.reset_scheduler_state_for_tests()
    session = DummySession()
    lease = FakeLease()
    lease.acquire()
    seen = []

    async def runner(db, *, settings):
        seen.append((db, settings))
        return {"status": "PAUSED", "dispatched": 0, "reason": "OPERATOR_PAUSE"}

    settings = _settings(routine_pinterest_scheduler_enabled=True)
    result = await scheduler.scheduler_tick(
        settings=settings,
        session_factory=lambda: session,
        runner=runner,
        leader_lease=lease,
    )

    assert result["status"] == "PAUSED"
    assert seen == [(session, settings)]
    assert session.closed is True
    status = scheduler.scheduler_status(settings)
    assert status["last_result"]["status"] == "PAUSED"
    assert status["lease_role"] == "leader"
    assert status["lease_held"] is True
    assert status["last_error"] is None


@pytest.mark.asyncio
async def test_lease_validation_loss_fails_closed_before_worker_session():
    scheduler.reset_scheduler_state_for_tests()
    lease = FakeLease(validate_result=False)
    lease.acquire()
    called = {"session": 0, "runner": 0}

    def session_factory():
        called["session"] += 1
        return DummySession()

    async def runner(*args, **kwargs):
        called["runner"] += 1
        return {"status": "SHOULD_NOT_RUN"}

    result = await scheduler.scheduler_tick(
        settings=_settings(routine_pinterest_scheduler_enabled=True),
        session_factory=session_factory,
        runner=runner,
        leader_lease=lease,
    )

    assert result == {"status": "SCHEDULER_LEASE_LOST", "dispatched": 0}
    assert called == {"session": 0, "runner": 0}
    status = scheduler.scheduler_status(_settings(routine_pinterest_scheduler_enabled=True))
    assert status["lease_role"] == "error"
    assert status["lease_held"] is False
    assert status["last_lease_status"] == "LOST"


@pytest.mark.asyncio
async def test_tick_overlap_is_rejected_without_second_worker_call():
    scheduler.reset_scheduler_state_for_tests()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []
    lease = FakeLease()
    lease.acquire()

    async def runner(db, *, settings):
        calls.append(db)
        entered.set()
        await release.wait()
        return {"status": "SUCCEEDED", "dispatched": 0}

    sessions = []

    def session_factory():
        session = DummySession()
        sessions.append(session)
        return session

    settings = _settings(routine_pinterest_scheduler_enabled=True)
    first = asyncio.create_task(
        scheduler.scheduler_tick(
            settings=settings,
            session_factory=session_factory,
            runner=runner,
            leader_lease=lease,
        )
    )
    await entered.wait()

    second = await scheduler.scheduler_tick(
        settings=settings,
        session_factory=session_factory,
        runner=runner,
        leader_lease=lease,
    )

    assert second == {"status": "SCHEDULER_TICK_ALREADY_RUNNING", "dispatched": 0}
    assert len(calls) == 1
    assert len(sessions) == 1
    release.set()
    await first
    assert sessions[0].closed is True


@pytest.mark.asyncio
async def test_scheduler_waits_before_first_lease_attempt_and_leader_tick():
    scheduler.reset_scheduler_state_for_tests()
    sleeper = ControlledSleep()
    lease = FakeLease()
    runner_called = asyncio.Event()
    calls = []

    async def runner(db, *, settings):
        calls.append(1)
        runner_called.set()
        return {"status": "WORKER_DISABLED", "dispatched": 0}

    settings = _settings(
        routine_pinterest_scheduler_enabled=True,
        routine_pinterest_scheduler_interval_seconds=60,
    )
    task = await scheduler.start_scheduler(
        settings=settings,
        session_factory=DummySession,
        runner=runner,
        sleep_fn=sleeper,
        lease_factory=lambda: lease,
    )
    await sleeper.entered.wait()

    assert task is not None
    assert calls == []
    assert lease.acquire_called.is_set() is False

    sleeper.release.set()
    await lease.acquire_called.wait()
    await runner_called.wait()
    assert calls == [1]

    await scheduler.stop_scheduler()
    assert lease.released is True
    status = scheduler.scheduler_status(settings)
    assert status["started"] is False
    assert status["task_running"] is False
    assert status["lease_held"] is False


@pytest.mark.asyncio
async def test_follower_standby_never_invokes_worker():
    scheduler.reset_scheduler_state_for_tests()
    sleeper = ControlledSleep()
    lease = FakeLease(acquire_status="STANDBY")
    called = {"session": 0, "runner": 0}

    def session_factory():
        called["session"] += 1
        return DummySession()

    async def runner(*args, **kwargs):
        called["runner"] += 1
        return {"status": "SHOULD_NOT_RUN"}

    settings = _settings(
        routine_pinterest_scheduler_enabled=True,
        routine_pinterest_scheduler_interval_seconds=60,
    )
    await scheduler.start_scheduler(
        settings=settings,
        session_factory=session_factory,
        runner=runner,
        sleep_fn=sleeper,
        lease_factory=lambda: lease,
    )
    await sleeper.entered.wait()
    sleeper.release.set()
    await lease.acquire_called.wait()
    await asyncio.sleep(0)

    assert called == {"session": 0, "runner": 0}
    status = scheduler.scheduler_status(settings)
    assert status["lease_role"] == "standby"
    assert status["lease_held"] is False

    await scheduler.stop_scheduler()


@pytest.mark.asyncio
@pytest.mark.parametrize("lease_status", ["UNSUPPORTED_BACKEND", "ERROR"])
async def test_lease_acquisition_failure_is_fail_closed(lease_status):
    scheduler.reset_scheduler_state_for_tests()
    sleeper = ControlledSleep()
    lease = FakeLease(
        acquire_status=lease_status,
        supported=lease_status != "UNSUPPORTED_BACKEND",
    )
    called = {"runner": 0}

    async def runner(*args, **kwargs):
        called["runner"] += 1
        return {"status": "SHOULD_NOT_RUN"}

    settings = _settings(
        routine_pinterest_scheduler_enabled=True,
        routine_pinterest_scheduler_interval_seconds=60,
    )
    await scheduler.start_scheduler(
        settings=settings,
        runner=runner,
        sleep_fn=sleeper,
        lease_factory=lambda: lease,
    )
    await sleeper.entered.wait()
    sleeper.release.set()
    await lease.acquire_called.wait()
    await asyncio.sleep(0)

    assert called["runner"] == 0
    status = scheduler.scheduler_status(settings)
    assert status["lease_role"] == "error"
    assert status["lease_held"] is False
    assert status["last_lease_status"] == lease_status

    await scheduler.stop_scheduler()


@pytest.mark.asyncio
async def test_start_is_idempotent_and_stop_releases_leader():
    scheduler.reset_scheduler_state_for_tests()
    sleeper = ControlledSleep()
    lease = FakeLease()
    runner_called = asyncio.Event()

    async def runner(db, *, settings):
        runner_called.set()
        return {"status": "WORKER_DISABLED", "dispatched": 0}

    settings = _settings(routine_pinterest_scheduler_enabled=True)
    first = await scheduler.start_scheduler(
        settings=settings,
        session_factory=DummySession,
        runner=runner,
        sleep_fn=sleeper,
        lease_factory=lambda: lease,
    )
    await sleeper.entered.wait()
    second = await scheduler.start_scheduler(
        settings=settings,
        session_factory=DummySession,
        runner=runner,
        sleep_fn=sleeper,
        lease_factory=lambda: FakeLease(),
    )
    assert first is second

    sleeper.release.set()
    await runner_called.wait()
    await scheduler.stop_scheduler()
    assert lease.released is True


@pytest.mark.asyncio
async def test_tick_error_is_observable_and_session_is_closed():
    scheduler.reset_scheduler_state_for_tests()
    session = DummySession()
    lease = FakeLease()
    lease.acquire()

    async def runner(db, *, settings):
        raise RuntimeError("synthetic")

    settings = _settings(routine_pinterest_scheduler_enabled=True)
    with pytest.raises(RuntimeError, match="synthetic"):
        await scheduler.scheduler_tick(
            settings=settings,
            session_factory=lambda: session,
            runner=runner,
            leader_lease=lease,
        )

    assert session.closed is True
    status = scheduler.scheduler_status(settings)
    assert status["last_error"] == "RuntimeError"
    assert status["last_result"] == {"status": "SCHEDULER_TICK_ERROR", "dispatched": 0}
    assert status["tick_running"] is False


def test_scheduler_status_contains_no_credentials():
    scheduler.reset_scheduler_state_for_tests()
    settings = _settings(
        buffer_api_key="secret-value",
        buffer_organization_id="org-1",
        buffer_pinterest_channel_id="channel-1",
    )
    status = scheduler.scheduler_status(settings)
    text = repr(status)
    assert "secret-value" not in text
    assert "org-1" not in text
    assert "channel-1" not in text
    assert status["lease_backend"] == "postgresql_advisory_lock"
    assert status["lease_held"] is False
