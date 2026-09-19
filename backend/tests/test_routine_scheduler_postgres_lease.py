import os

import pytest
from sqlalchemy import create_engine, text

from app.services.routine_scheduler_lease import (
    PostgresSchedulerLeaderLease,
    ROUTINE_SCHEDULER_ADVISORY_LOCK_KEY,
)


POSTGRES_URL = os.getenv("TASK46_POSTGRES_URL")


@pytest.mark.skipif(not POSTGRES_URL, reason="TASK46_POSTGRES_URL not configured")
def test_postgres_advisory_leader_is_exclusive_and_fails_over():
    engine = create_engine(POSTGRES_URL, pool_pre_ping=True)
    leader = PostgresSchedulerLeaderLease(engine)
    follower = PostgresSchedulerLeaderLease(engine)
    try:
        assert leader.acquire() == "ACQUIRED"
        assert leader.held is True
        assert leader.backend_pid is not None
        assert leader.validate() is True

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


@pytest.mark.skipif(not POSTGRES_URL, reason="TASK46_POSTGRES_URL not configured")
def test_postgres_connection_close_naturally_releases_leader_lock():
    engine = create_engine(POSTGRES_URL, pool_pre_ping=True)
    first = engine.connect()
    second = engine.connect()
    try:
        first_pid = int(first.execute(text("SELECT pg_backend_pid()")).scalar_one())
        second_pid = int(second.execute(text("SELECT pg_backend_pid()")).scalar_one())
        assert first_pid != second_pid

        assert bool(first.execute(
            text("SELECT pg_try_advisory_lock(:key)"),
            {"key": ROUTINE_SCHEDULER_ADVISORY_LOCK_KEY},
        ).scalar_one()) is True
        first.detach()
        assert bool(second.execute(
            text("SELECT pg_try_advisory_lock(:key)"),
            {"key": ROUTINE_SCHEDULER_ADVISORY_LOCK_KEY},
        ).scalar_one()) is False

        # Session advisory locks are released by PostgreSQL when the owning
        # connection closes, which provides crash/process-loss failover.
        first.close()

        assert bool(second.execute(
            text("SELECT pg_try_advisory_lock(:key)"),
            {"key": ROUTINE_SCHEDULER_ADVISORY_LOCK_KEY},
        ).scalar_one()) is True
        assert int(second.execute(text("SELECT pg_backend_pid()")).scalar_one()) == second_pid
    finally:
        if not first.closed:
            first.close()
        try:
            second.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": ROUTINE_SCHEDULER_ADVISORY_LOCK_KEY},
            )
        except Exception:
            pass
        second.close()
        engine.dispose()


def test_non_postgres_backend_fails_closed_without_connection():
    engine = create_engine("sqlite:///:memory:")
    lease = PostgresSchedulerLeaderLease(engine)
    assert lease.supported is False
    assert lease.acquire() == "UNSUPPORTED_BACKEND"
    assert lease.held is False
    assert lease.connection is None
    assert lease.last_error == "POSTGRESQL_REQUIRED"
    engine.dispose()
