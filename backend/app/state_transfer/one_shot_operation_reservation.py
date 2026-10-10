"""Durable PostgreSQL reservation primitive for an explicitly authorized one-shot.

This module DOES NOT create its table, open a batch, invoke preparation, or
publish. A separately reviewed migration and execution runner are required.
No automatic retry is permitted after a duplicate or ambiguous result.
"""
from __future__ import annotations

import re
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

HEX64 = re.compile(r"[0-9a-f]{64}\Z")
OPERATION_KEY = re.compile(r"task61-76-[a-z0-9-]{1,72}\Z")

metadata = sa.MetaData()
reservations = sa.Table(
    "routine_one_shot_preparation_operations", metadata,
    sa.Column("operation_key", sa.String(96), primary_key=True),
    sa.Column("preflight_fingerprint", sa.String(64), nullable=False),
    sa.Column("batch_id", sa.String(36), nullable=False, unique=True),
    sa.Column("state", sa.String(20), nullable=False, server_default="RESERVED"),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
              server_default=sa.func.now()),
    sa.CheckConstraint(
        "state IN ('RESERVED','PREPARING','READY','FAILED','UNCERTAIN')",
        name="ck_one_shot_preparation_state",
    ),
    sa.CheckConstraint(
        "length(preflight_fingerprint) = 64",
        name="ck_one_shot_preparation_fingerprint_length",
    ),
)


class OneShotReservationRefusal(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def reserve_once(db, *, operation_key: str, preflight_fingerprint: str,
                 batch_id: str) -> None:
    """Persist reservation before first batch write; never re-admit duplicates.

    This must run in its own committed transaction. No exceptions are treated
    as retry permission: a lost acknowledgment may mean the commit succeeded.
    """
    if db.get_bind().dialect.name != "postgresql":
        raise OneShotReservationRefusal("ONE_SHOT_POSTGRES_REQUIRED")
    if not isinstance(operation_key, str) or not OPERATION_KEY.fullmatch(operation_key):
        raise OneShotReservationRefusal("ONE_SHOT_OPERATION_KEY_INVALID")
    if not isinstance(preflight_fingerprint, str) or not HEX64.fullmatch(preflight_fingerprint):
        raise OneShotReservationRefusal("ONE_SHOT_PREFLIGHT_FINGERPRINT_INVALID")
    if not isinstance(batch_id, str) or not 1 <= len(batch_id) <= 36:
        raise OneShotReservationRefusal("ONE_SHOT_BATCH_ID_INVALID")
    statement = insert(reservations).values(
        operation_key=operation_key,
        preflight_fingerprint=preflight_fingerprint,
        batch_id=batch_id,
        state="RESERVED",
    ).on_conflict_do_nothing(index_elements=["operation_key"])
    try:
        inserted = db.execute(statement).rowcount
        db.commit()
    except Exception as exc:
        db.rollback()
        raise OneShotReservationRefusal("ONE_SHOT_RESERVATION_UNCERTAIN") from None
    if inserted != 1:
        raise OneShotReservationRefusal("ONE_SHOT_OPERATION_ALREADY_CONSUMED")


def mark_outcome(db, *, operation_key: str, expected_state: str,
                 next_state: str) -> None:
    """Compare-and-set only. No completion or retry without durable state."""
    allowed = {
        ("RESERVED", "PREPARING"),
        ("RESERVED", "FAILED"),
        ("RESERVED", "UNCERTAIN"),
        ("PREPARING", "READY"),
        ("PREPARING", "FAILED"),
        ("PREPARING", "UNCERTAIN"),
    }
    if not isinstance(operation_key, str) or not OPERATION_KEY.fullmatch(operation_key):
        raise OneShotReservationRefusal("ONE_SHOT_OPERATION_KEY_INVALID")
    if (expected_state, next_state) not in allowed:
        raise OneShotReservationRefusal("ONE_SHOT_TRANSITION_FORBIDDEN")
    if db.get_bind().dialect.name != "postgresql":
        raise OneShotReservationRefusal("ONE_SHOT_POSTGRES_REQUIRED")
    try:
        changed = db.execute(
            sa.update(reservations).where(
                reservations.c.operation_key == operation_key,
                reservations.c.state == expected_state,
            ).values(state=next_state)
        ).rowcount
        if changed != 1:
            db.rollback()
            raise OneShotReservationRefusal("ONE_SHOT_STATE_MISMATCH")
        db.commit()
    except OneShotReservationRefusal:
        raise
    except Exception:
        db.rollback()
        raise OneShotReservationRefusal("ONE_SHOT_TRANSITION_UNCERTAIN") from None
