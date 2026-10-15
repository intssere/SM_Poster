"""Read-only one-shot reservation inspection for recovery and operator review.

No execution, transitions, retries, provider calls, or writes. An uncertain
reservation is never interpreted as permission to prepare another batch.
"""
from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa

from .one_shot_operation_reservation import (
    OPERATION_KEY, OneShotReservationRefusal, reservations,
)


@dataclass(frozen=True)
class OneShotReservationSnapshot:
    operation_key: str
    preflight_fingerprint: str
    batch_id: str
    state: str


def inspect_reservation(db, *, operation_key: str) -> OneShotReservationSnapshot | None:
    """Inspect durable state using a caller-managed read-only transaction."""
    if not isinstance(operation_key, str) or not OPERATION_KEY.fullmatch(operation_key):
        raise OneShotReservationRefusal("ONE_SHOT_OPERATION_KEY_INVALID")
    if db.get_bind().dialect.name != "postgresql":
        raise OneShotReservationRefusal("ONE_SHOT_POSTGRES_REQUIRED")
    row = db.execute(sa.select(
        reservations.c.operation_key,
        reservations.c.preflight_fingerprint,
        reservations.c.batch_id,
        reservations.c.state,
    ).where(reservations.c.operation_key == operation_key)).mappings().one_or_none()
    if row is None:
        return None
    if row["state"] not in {"RESERVED", "PREPARING", "READY", "FAILED", "UNCERTAIN"}:
        raise OneShotReservationRefusal("ONE_SHOT_STATE_INVALID")
    return OneShotReservationSnapshot(**row)
