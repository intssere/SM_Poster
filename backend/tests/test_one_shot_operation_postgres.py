"""PostgreSQL-backed isolated one-shot reservation tests.

No production schema and no application batch tables are touched. The temporary
test schema is created and discarded on the CI database only.
"""
from __future__ import annotations

import os
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.state_transfer.one_shot_operation_reservation import (
    OneShotReservationRefusal, metadata, reservations, reserve_once, mark_outcome,
)

KEY = "task61-76-ci-reservation"
FP = "a" * 64


@pytest.fixture
def pg_reservations():
    dsn = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("PostgreSQL CI database URL unavailable")
    engine = sa.create_engine(dsn, pool_pre_ping=True)
    if engine.dialect.name != "postgresql":
        engine.dispose()
        pytest.skip("Only PostgreSQL exercises this contract")
    schema = "test_one_shot_" + uuid4().hex
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        scoped = engine.execution_options(schema_translate_map={None: schema})
        metadata.create_all(scoped)
        yield scoped
    finally:
        with engine.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()


def test_postgres_duplicate_key_is_durably_refused(pg_reservations):
    batch1, batch2 = str(uuid4()), str(uuid4())
    with Session(pg_reservations) as db:
        reserve_once(db, operation_key=KEY, preflight_fingerprint=FP, batch_id=batch1)
    with Session(pg_reservations) as db:
        with pytest.raises(OneShotReservationRefusal, match="ONE_SHOT_OPERATION_ALREADY_CONSUMED"):
            reserve_once(db, operation_key=KEY, preflight_fingerprint=FP, batch_id=batch2)
    with Session(pg_reservations) as db:
        rows = db.execute(sa.select(reservations)).mappings().all()
        assert len(rows) == 1
        assert rows[0]["batch_id"] == batch1
        assert rows[0]["state"] == "RESERVED"


def test_postgres_state_cas_and_terminal_no_reopen(pg_reservations):
    with Session(pg_reservations) as db:
        reserve_once(db, operation_key=KEY, preflight_fingerprint=FP, batch_id=str(uuid4()))
        mark_outcome(db, operation_key=KEY, expected_state="RESERVED", next_state="PREPARING")
        mark_outcome(db, operation_key=KEY, expected_state="PREPARING", next_state="UNCERTAIN")
    with Session(pg_reservations) as db:
        with pytest.raises(OneShotReservationRefusal, match="ONE_SHOT_STATE_MISMATCH"):
            mark_outcome(db, operation_key=KEY, expected_state="PREPARING", next_state="READY")
        actual = db.execute(sa.select(reservations.c.state)).scalar_one()
        assert actual == "UNCERTAIN"


def test_postgres_batch_identity_cannot_be_reused(pg_reservations):
    batch_id = str(uuid4())
    with Session(pg_reservations) as db:
        reserve_once(db, operation_key=KEY, preflight_fingerprint=FP, batch_id=batch_id)
    with Session(pg_reservations) as db:
        with pytest.raises(OneShotReservationRefusal, match="ONE_SHOT_RESERVATION_UNCERTAIN"):
            reserve_once(
                db, operation_key="task61-76-other-attempt",
                preflight_fingerprint=FP, batch_id=batch_id,
            )
        assert db.execute(sa.select(sa.func.count()).select_from(reservations)).scalar_one() == 1
