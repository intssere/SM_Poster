from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine

# Fixed application-owned advisory-lock key. This value is intentionally
# configuration-independent so every process competes for the same leader slot.
ROUTINE_SCHEDULER_ADVISORY_LOCK_KEY = 460817247163244653
LEASE_BACKEND = "postgresql_advisory_lock"


def _utcnow():
    return datetime.now(timezone.utc)


class PostgresSchedulerLeaderLease:
    """Process leader lease backed by one PostgreSQL session advisory lock.

    The dedicated connection is held for the entire leadership lifetime. Worker
    database sessions remain separate, so their commits cannot affect lease
    ownership.
    """

    def __init__(self, engine: Engine, *, lock_key: int = ROUTINE_SCHEDULER_ADVISORY_LOCK_KEY):
        self.engine = engine
        self.lock_key = lock_key
        self.connection = None
        self.backend_pid: int | None = None
        self.acquired_at = None
        self.lost_at = None
        self.last_status = "NOT_ATTEMPTED"
        self.last_error: str | None = None

    @property
    def supported(self) -> bool:
        return self.engine.dialect.name == "postgresql"

    @property
    def held(self) -> bool:
        return self.connection is not None and self.backend_pid is not None and self.last_status == "ACQUIRED"

    def acquire(self) -> str:
        if self.held:
            return "ACQUIRED"
        if not self.supported:
            self.last_status = "UNSUPPORTED_BACKEND"
            self.last_error = "POSTGRESQL_REQUIRED"
            return self.last_status

        connection = None
        try:
            connection = self.engine.connect()
            backend_pid = int(connection.execute(text("SELECT pg_backend_pid()")).scalar_one())
            acquired = bool(
                connection.execute(
                    text("SELECT pg_try_advisory_lock(:lock_key)"),
                    {"lock_key": self.lock_key},
                ).scalar_one()
            )
            if not acquired:
                connection.close()
                self.last_status = "STANDBY"
                self.last_error = None
                return self.last_status

            self.connection = connection
            self.backend_pid = backend_pid
            self.acquired_at = _utcnow()
            self.last_status = "ACQUIRED"
            self.last_error = None
            return self.last_status
        except Exception as exc:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            self.connection = None
            self.backend_pid = None
            self.last_status = "ERROR"
            self.last_error = exc.__class__.__name__
            return self.last_status

    def validate(self) -> bool:
        if not self.held:
            return False
        try:
            observed = int(self.connection.execute(text("SELECT pg_backend_pid()")).scalar_one())
            if observed != self.backend_pid:
                self._mark_lost("BACKEND_PID_CHANGED")
                return False
            return True
        except Exception as exc:
            self._mark_lost(exc.__class__.__name__)
            return False

    def _mark_lost(self, error: str):
        connection = self.connection
        self.connection = None
        self.backend_pid = None
        self.lost_at = _utcnow()
        self.last_status = "LOST"
        self.last_error = error
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    def release(self):
        connection = self.connection
        backend_pid = self.backend_pid
        self.connection = None
        self.backend_pid = None
        if connection is None:
            if self.last_status == "ACQUIRED":
                self.last_status = "RELEASED"
            return
        try:
            # Only unlock if this is still the same PostgreSQL session. If the
            # connection has been transparently replaced, closing it is the safe
            # fail-closed behavior and we must not unlock another session.
            observed = int(connection.execute(text("SELECT pg_backend_pid()")).scalar_one())
            if observed == backend_pid:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": self.lock_key},
                ).scalar_one()
            else:
                self.last_error = "BACKEND_PID_CHANGED"
        except Exception as exc:
            self.last_error = exc.__class__.__name__
        finally:
            try:
                connection.close()
            except Exception:
                pass
            self.last_status = "RELEASED"
