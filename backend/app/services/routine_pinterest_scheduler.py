from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from app.core.config import Settings, get_settings
from app.db.session import SessionLocal, engine
from app.services.routine_pinterest_worker import run_once as run_routine_worker_once
from app.services.routine_scheduler_lease import (
    LEASE_BACKEND,
    PostgresSchedulerLeaderLease,
)

SleepFn = Callable[[float], Awaitable[Any]]

_task: asyncio.Task | None = None
_tick_lock: asyncio.Lock | None = None
_leader_lease = None
_state = {
    "started": False,
    "tick_running": False,
    "last_tick_started_at": None,
    "last_tick_completed_at": None,
    "last_result": None,
    "last_error": None,
    "lease_supported": engine.dialect.name == "postgresql",
    "lease_role": "disabled",
    "lease_held": False,
    "last_lease_status": "NOT_ATTEMPTED",
    "last_lease_acquired_at": None,
    "last_lease_lost_at": None,
    "last_lease_error": None,
}


def _utcnow():
    return datetime.now(timezone.utc)


def _get_tick_lock() -> asyncio.Lock:
    global _tick_lock
    if _tick_lock is None:
        _tick_lock = asyncio.Lock()
    return _tick_lock


def _default_lease_factory():
    return PostgresSchedulerLeaderLease(engine)


def _record_lease(lease, *, role: str | None = None):
    _state["lease_supported"] = bool(getattr(lease, "supported", False))
    _state["lease_held"] = bool(getattr(lease, "held", False))
    status = getattr(lease, "last_status", "ERROR")
    _state["last_lease_status"] = status
    _state["last_lease_error"] = getattr(lease, "last_error", None)
    acquired_at = getattr(lease, "acquired_at", None)
    lost_at = getattr(lease, "lost_at", None)
    if acquired_at is not None:
        _state["last_lease_acquired_at"] = acquired_at
    if lost_at is not None:
        _state["last_lease_lost_at"] = lost_at
    if role is not None:
        _state["lease_role"] = role
    elif _state["lease_held"]:
        _state["lease_role"] = "leader"
    elif status == "STANDBY":
        _state["lease_role"] = "standby"
    elif status in {"ERROR", "UNSUPPORTED_BACKEND", "LOST"}:
        _state["lease_role"] = "error"
    else:
        _state["lease_role"] = "waiting"


def scheduler_status(settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    task_running = bool(_task and not _task.done())
    role = _state["lease_role"]
    if settings.routine_pinterest_scheduler_enabled is not True:
        role = "disabled" if not _state["lease_held"] else role
    elif role == "disabled":
        role = "waiting"
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
        "lease_required": settings.routine_pinterest_scheduler_enabled is True,
        "lease_backend": LEASE_BACKEND,
        "lease_supported": bool(_state["lease_supported"]),
        "lease_role": role,
        "lease_held": bool(_state["lease_held"]),
        "last_lease_status": _state["last_lease_status"],
        "last_lease_acquired_at": _state["last_lease_acquired_at"],
        "last_lease_lost_at": _state["last_lease_lost_at"],
        "last_lease_error": _state["last_lease_error"],
    }


async def scheduler_tick(
    *,
    settings: Settings | None = None,
    session_factory=SessionLocal,
    runner=run_routine_worker_once,
    leader_lease=None,
) -> dict:
    settings = settings or get_settings()
    if settings.routine_pinterest_scheduler_enabled is not True:
        return {"status": "SCHEDULER_DISABLED", "dispatched": 0}

    lock = _get_tick_lock()
    if lock.locked():
        return {"status": "SCHEDULER_TICK_ALREADY_RUNNING", "dispatched": 0}

    async with lock:
        if leader_lease is None or not bool(getattr(leader_lease, "held", False)):
            result = {"status": "SCHEDULER_LEASE_NOT_HELD", "dispatched": 0}
            _state["last_result"] = result
            return result
        if not leader_lease.validate():
            _record_lease(leader_lease, role="error")
            result = {"status": "SCHEDULER_LEASE_LOST", "dispatched": 0}
            _state["last_result"] = result
            _state["last_error"] = "SCHEDULER_LEASE_LOST"
            return result

        _record_lease(leader_lease, role="leader")
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
    lease_factory=_default_lease_factory,
):
    global _leader_lease
    lease = None
    try:
        while True:
            # Preserve Task #44 semantics: enabling the scheduler never causes an
            # immediate lease attempt or provider-facing worker action at startup.
            await sleep_fn(settings.routine_pinterest_scheduler_interval_seconds)

            if lease is None or not bool(getattr(lease, "held", False)):
                candidate = lease_factory()
                status = candidate.acquire()
                _record_lease(candidate)
                if status != "ACQUIRED":
                    # STANDBY and error/unsupported states are fail-closed: no
                    # worker session is opened and no routine run is started.
                    continue
                lease = candidate
                _leader_lease = lease
                _record_lease(lease, role="leader")

            result = await scheduler_tick(
                settings=settings,
                session_factory=session_factory,
                runner=runner,
                leader_lease=lease,
            )
            if result.get("status") == "SCHEDULER_LEASE_LOST" or not lease.held:
                _leader_lease = None
                lease = None
    except asyncio.CancelledError:
        raise
    finally:
        if lease is not None:
            lease.release()
            _record_lease(lease, role="stopped")
        _leader_lease = None


async def start_scheduler(
    *,
    settings: Settings | None = None,
    session_factory=SessionLocal,
    runner=run_routine_worker_once,
    sleep_fn: SleepFn = asyncio.sleep,
    lease_factory=_default_lease_factory,
):
    global _task
    settings = settings or get_settings()
    if settings.routine_pinterest_scheduler_enabled is not True:
        _state["started"] = False
        _state["lease_role"] = "disabled"
        _state["lease_held"] = False
        return None
    if _task and not _task.done():
        return _task
    _task = asyncio.create_task(
        _scheduler_loop(
            settings=settings,
            session_factory=session_factory,
            runner=runner,
            sleep_fn=sleep_fn,
            lease_factory=lease_factory,
        ),
        name="routine-pinterest-scheduler",
    )
    _state["started"] = True
    _state["lease_role"] = "waiting"
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
    except Exception:
        # Scheduler failures are already captured in observable state. Shutdown
        # must still complete so the loop's finally block releases leadership.
        pass


def reset_scheduler_state_for_tests():
    global _task, _tick_lock, _leader_lease
    if _task and not _task.done():
        _task.cancel()
    _task = None
    _tick_lock = None
    if _leader_lease is not None:
        try:
            _leader_lease.release()
        except Exception:
            pass
    _leader_lease = None
    _state.update({
        "started": False,
        "tick_running": False,
        "last_tick_started_at": None,
        "last_tick_completed_at": None,
        "last_result": None,
        "last_error": None,
        "lease_supported": engine.dialect.name == "postgresql",
        "lease_role": "disabled",
        "lease_held": False,
        "last_lease_status": "NOT_ATTEMPTED",
        "last_lease_acquired_at": None,
        "last_lease_lost_at": None,
        "last_lease_error": None,
    })
