import asyncio
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.services import routine_pinterest_scheduler as scheduler


class DummySession:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


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
async def test_disabled_scheduler_does_not_start_or_tick():
    scheduler.reset_scheduler_state_for_tests()
    called = {"session": 0, "runner": 0}

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
    )
    result = await scheduler.scheduler_tick(
        settings=_settings(),
        session_factory=session_factory,
        runner=runner,
    )

    assert task is None
    assert result == {"status": "SCHEDULER_DISABLED", "dispatched": 0}
    assert called == {"session": 0, "runner": 0}
    status = scheduler.scheduler_status(_settings())
    assert status["enabled"] is False
    assert status["started"] is False
    assert status["task_running"] is False


@pytest.mark.asyncio
async def test_enabled_tick_reuses_worker_and_closes_session():
    scheduler.reset_scheduler_state_for_tests()
    session = DummySession()
    seen = []

    async def runner(db, *, settings):
        seen.append((db, settings))
        return {"status": "PAUSED", "dispatched": 0, "reason": "OPERATOR_PAUSE"}

    settings = _settings(routine_pinterest_scheduler_enabled=True)
    result = await scheduler.scheduler_tick(
        settings=settings,
        session_factory=lambda: session,
        runner=runner,
    )

    assert result["status"] == "PAUSED"
    assert result["dispatched"] == 0
    assert seen == [(session, settings)]
    assert session.closed is True
    status = scheduler.scheduler_status(settings)
    assert status["last_result"]["status"] == "PAUSED"
    assert status["last_error"] is None
    assert status["tick_running"] is False


@pytest.mark.asyncio
async def test_tick_overlap_is_rejected_without_second_worker_call():
    scheduler.reset_scheduler_state_for_tests()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

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
        )
    )
    await entered.wait()

    second = await scheduler.scheduler_tick(
        settings=settings,
        session_factory=session_factory,
        runner=runner,
    )

    assert second == {"status": "SCHEDULER_TICK_ALREADY_RUNNING", "dispatched": 0}
    assert len(calls) == 1
    assert len(sessions) == 1

    release.set()
    await first
    assert sessions[0].closed is True


@pytest.mark.asyncio
async def test_scheduler_waits_before_first_tick_and_stops_cleanly():
    scheduler.reset_scheduler_state_for_tests()
    sleep_entered = asyncio.Event()
    runner_called = asyncio.Event()
    release_sleep = asyncio.Event()
    calls = []

    async def sleep_fn(seconds):
        assert seconds == 60
        sleep_entered.set()
        await release_sleep.wait()
        release_sleep.clear()

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
        sleep_fn=sleep_fn,
    )
    await sleep_entered.wait()

    assert task is not None
    assert calls == []
    assert scheduler.scheduler_status(settings)["task_running"] is True

    release_sleep.set()
    await runner_called.wait()
    assert calls == [1]

    await scheduler.stop_scheduler()
    status = scheduler.scheduler_status(settings)
    assert status["started"] is False
    assert status["task_running"] is False


@pytest.mark.asyncio
async def test_start_is_idempotent_while_scheduler_task_is_running():
    scheduler.reset_scheduler_state_for_tests()
    sleep_entered = asyncio.Event()
    release = asyncio.Event()

    async def sleep_fn(seconds):
        sleep_entered.set()
        await release.wait()

    settings = _settings(routine_pinterest_scheduler_enabled=True)
    first = await scheduler.start_scheduler(settings=settings, sleep_fn=sleep_fn)
    await sleep_entered.wait()
    second = await scheduler.start_scheduler(settings=settings, sleep_fn=sleep_fn)

    assert first is second
    await scheduler.stop_scheduler()


@pytest.mark.asyncio
async def test_tick_error_is_observable_and_session_is_closed():
    scheduler.reset_scheduler_state_for_tests()
    session = DummySession()

    async def runner(db, *, settings):
        raise RuntimeError("synthetic")

    settings = _settings(routine_pinterest_scheduler_enabled=True)
    with pytest.raises(RuntimeError, match="synthetic"):
        await scheduler.scheduler_tick(
            settings=settings,
            session_factory=lambda: session,
            runner=runner,
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
