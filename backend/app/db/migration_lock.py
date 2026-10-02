"""Shared migration lock namespace and bounded transaction-owned acquisition."""
import math
import time
import sqlalchemy as sa

MIGRATION_ADVISORY_LOCK_KEY = 490019
_PROOF = object()
_INFO_KEY = "exact_0032_transaction_lock"


def _owned(connection) -> bool:
    return connection.scalar(sa.text(
        "SELECT count(*) FROM pg_locks WHERE locktype='advisory' "
        "AND pid=pg_backend_pid() AND classid=0 AND objid=:key "
        "AND objsubid=1 AND granted"
    ), {"key": MIGRATION_ADVISORY_LOCK_KEY}) == 1


def acquire_transaction_lock(connection, timeout: float) -> None:
    if not math.isfinite(timeout) or not 0 < timeout <= 30:
        raise ValueError("lock wait must be finite and between zero and 30 seconds")
    if not connection.in_transaction() or _owned(connection):
        raise RuntimeError("ambiguous migration lock ownership")
    deadline = time.monotonic() + timeout
    while True:
        acquired = connection.scalar(sa.text(
            "SELECT pg_try_advisory_xact_lock(:key)"
        ), {"key": MIGRATION_ADVISORY_LOCK_KEY})
        if acquired:
            connection.info[_INFO_KEY] = (_PROOF, connection.get_transaction())
            require_transaction_lock(connection)
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("migration lock contention")
        time.sleep(min(0.05, remaining))


def require_transaction_lock(connection) -> None:
    proof = connection.info.get(_INFO_KEY)
    if (
        not connection.in_transaction()
        or proof != (_PROOF, connection.get_transaction())
        or not _owned(connection)
    ):
        raise RuntimeError("caller-owned migration transaction lock required")


def clear_lock_proof(connection) -> None:
    connection.info.pop(_INFO_KEY, None)