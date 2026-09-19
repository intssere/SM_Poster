from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from app.core.config import Settings, get_settings
from app.db.session import SessionLocal
from app.services.routine_pinterest_worker import run_once as run_routine_worker_once

SleepFn = Callable[[float], Awaitable[Any]]

_task: asyncio.Task | None = None
_tick_lock: asyncio.Lock | None = None
_state = {
    "started": False,
    "tick_running": False,
    "last_tick_started_at": None,
    "last_tick_completed_at": None,
    "last_result": None,
    "last_error": None,
}


def _utcnow():
    return datetime.now(timezone.utc)


def _get_tick_lock() -> asyncio.Lock:
    global _tick_lock
    if _tick_lock is None:
        _tick_lock = asyncio.Lock()
    return _tick_lock


def scheduler_status(settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    task_running = bool(_task and not _task.done())
    return {
        "enabled": settings.routine_pinterest_scheduler_enabled,
        "interval_seconds": settings.routine_pinterest_scheduler_interval_seconds,
        "started": bool(_state["started"]),
        "task_running": task_running,
        "tick_running": bool(_state["tick_running"]),
        "last_tick_started_at": _state["last_tick_started_at"],
        "last_tick_completed_at": _state["last_tick_completed_at"],
        "last_result": _state["last_result"],
        "last_error": _state["last_error"],
    }


async def scheduler_tick(
    *,
    settings: Settings | None = None,
    session_factory=SessionLocal,
    runner=run_routine_worker_once,
) -> dict:
    settings = settings or get_settings()
    if settings.routine_pinterest_scheduler_enabled is not True:
        return {"status": "SCHEDULER_DISABLED", "dispatched": 0}

    lock = _get_tick_lock()
    if lock.locked():
        return {"status": "SCHEDULER_TICK_ALREADY_RUNNING", "dispatched": 0}

    async with lock:
        _state["tick_running"] = True
        _state["last_tick_started_at"] = _utcnow()
        _state["last_error"] = None
        db = session_factory()
        try:
            result = await runner(db, settings=settings)
            _state["last_result"] = result
            return result
        except Exception as exc:
            _state["last_error"] = exc.__class__.__name__
            _state["last_result"] = {"status": "SCHEDULER_TICK_ERROR", "dispatched": 0}
            raise
        finally:
            db.close()
            _state["last_tick_completed_at"] = _utcnow()
            _state["tick_running"] = False


async def _scheduler_loop(
    *,
    settings: Settings,
    session_factory=SessionLocal,
    runner=run_routine_worker_once,
    sleep_fn: SleepFn = asyncio.sleep,
):
    try:
        while True:
            # Intentionally delay the first tick. Enabling the scheduler should
            # never cause an immediate provider-facing action at process start.
            await sleep_fn(settings.routine_pinterest_scheduler_interval_seconds)
            await scheduler_tick(
                settings=settings,
                session_factory=session_factory,
                runner=runner,
            )
    except asyncio.CancelledError:
        raise


async def start_scheduler(
    *,
    settings: Settings | None = None,
    session_factory=SessionLocal,
    runner=run_routine_worker_once,
    sleep_fn: SleepFn = asyncio.sleep,
):
    global _task
    settings = settings or get_settings()
    if settings.routine_pinterest_scheduler_enabled is not True:
        _state["started"] = False
        return None
    if _task and not _task.done():
        return _task
    _task = asyncio.create_task(
        _scheduler_loop(
            settings=settings,
            session_factory=session_factory,
            runner=runner,
            sleep_fn=sleep_fn,
        ),
        name="routine-pinterest-scheduler",
    )
    _state["started"] = True
    return _task


async def stop_scheduler():
    global _task
    task = _task
    _task = None
    _state["started"] = False
    if task is None:
        return
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def reset_scheduler_state_for_tests():
    global _task, _tick_lock
    if _task and not _task.done():
        _task.cancel()
    _task = None
    _tick_lock = None
    _state.update({
        "started": False,
        "tick_running": False,
        "last_tick_started_at": None,
        "last_tick_completed_at": None,
        "last_result": None,
        "last_error": None,
    })
