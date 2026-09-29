from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models.routine_publishing import (
    RoutinePublishingControl,
    RoutineScheduledQuotaReservation,
)
from app.services.routine_scheduled_quotas import (
    ScheduledQuotaError,
    ScheduledQuotaLimits,
    assess_scheduled_quota,
    reserve_scheduled_quota,
)


class _PostgresReservationSession:
    """Exercise transaction/query logic on isolated SQLite without claiming lock coverage."""

    def __init__(self, session):
        self._session = session

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def __getattr__(self, name):
        return getattr(self._session, name)


@pytest.fixture
def isolated_db():
    engine = create_engine("sqlite://")
    RoutinePublishingControl.__table__.create(engine)
    RoutineScheduledQuotaReservation.__table__.create(engine)
    session = Session(engine)
    session.add(RoutinePublishingControl(id="default", state="PAUSED"))
    session.commit()
    yield session
    session.close()
    engine.dispose()


def _reserve(db, *, publication="pub-1", day=date(2026, 4, 10), **overrides):
    values = {
        "publication_id": publication,
        "plan_id": "plan-1",
        "plan_item_id": f"item-{publication}",
        "product_id": "product-1",
        "vendor_key": "Acme",
        "board_id": "board-1",
        "scheduled_for": day,
        "limits": ScheduledQuotaLimits(
            daily=10, monthly=10, product=10, vendor=10, board=10
        ),
    }
    values.update(overrides)
    return reserve_scheduled_quota(db, **values)


def test_actual_reservation_fails_closed_on_unsupported_database(isolated_db):
    with pytest.raises(ScheduledQuotaError) as error:
        _reserve(isolated_db)
    assert error.value.code == "SCHEDULED_QUOTA_DATABASE_UNSUPPORTED"
    assert isolated_db.scalar(select(RoutineScheduledQuotaReservation)) is None


def test_reservation_is_idempotent_and_conflicting_identity_fails_closed(isolated_db):
    db = _PostgresReservationSession(isolated_db)
    first = _reserve(db)
    duplicate = _reserve(db)
    assert duplicate.id == first.id
    assert isolated_db.scalar(
        select(RoutineScheduledQuotaReservation)
    ).publication_id == "pub-1"

    with pytest.raises(ScheduledQuotaError) as error:
        _reserve(db, publication="pub-1", day=date(2026, 4, 11))
    assert error.value.code == "SCHEDULED_QUOTA_PUBLICATION_IDENTITY_CONFLICT"


@pytest.mark.parametrize(
    ("quota", "first_day", "second_day", "second_identity", "expected_code"),
    [
        ("daily", date(2026, 4, 10), date(2026, 4, 10), {}, "SCHEDULED_QUOTA_DAILY_LIMIT"),
        ("monthly", date(2026, 4, 10), date(2026, 4, 11), {}, "SCHEDULED_QUOTA_MONTHLY_LIMIT"),
        (
            "product",
            date(2026, 4, 10),
            date(2026, 4, 11),
            {"product_id": "product-1"},
            "SCHEDULED_QUOTA_PRODUCT_LIMIT",
        ),
        (
            "vendor",
            date(2026, 4, 10),
            date(2026, 4, 11),
            {"vendor_key": " acme "},
            "SCHEDULED_QUOTA_VENDOR_LIMIT",
        ),
        (
            "board",
            date(2026, 4, 10),
            date(2026, 4, 11),
            {"board_id": "board-1"},
            "SCHEDULED_QUOTA_BOARD_LIMIT",
        ),
    ],
)
def test_each_quota_dimension_is_enforced(
    isolated_db, quota, first_day, second_day, second_identity, expected_code
):
    db = _PostgresReservationSession(isolated_db)
    limits = ScheduledQuotaLimits(
        daily=1 if quota == "daily" else 20,
        monthly=1 if quota == "monthly" else 20,
        product=1 if quota == "product" else 20,
        vendor=1 if quota == "vendor" else 20,
        board=1 if quota == "board" else 20,
    )
    _reserve(db, day=first_day, limits=limits)
    second = {
        "publication_id": "pub-2",
        "plan_id": "plan-2",
        "plan_item_id": "item-2",
        "product_id": "product-other",
        "vendor_key": "Other",
        "board_id": "board-other",
        "scheduled_for": second_day,
        "limits": limits,
    }
    second.update(second_identity)
    with pytest.raises(ScheduledQuotaError) as error:
        reserve_scheduled_quota(db, **second)
    assert error.value.code == expected_code


def test_reservation_uses_caller_transaction_and_rollback_removes_it(isolated_db):
    db = _PostgresReservationSession(isolated_db)
    reservation = _reserve(db)
    reservation_id = reservation.id
    isolated_db.rollback()
    assert isolated_db.get(RoutineScheduledQuotaReservation, reservation_id) is None


def test_month_bucket_is_derived_from_scheduled_day(isolated_db):
    reservation = _reserve(_PostgresReservationSession(isolated_db))
    assert reservation.month_start == date(2026, 4, 1)


def test_read_only_assessment_reports_all_headroom_without_writing(isolated_db):
    assessment = assess_scheduled_quota(
        isolated_db,
        publication_id="pub-assessment",
        plan_id="plan-1",
        plan_item_id="item-assessment",
        product_id="product-1",
        vendor_key="Acme",
        board_id="board-1",
        scheduled_for=date(2026, 4, 10),
        limits=ScheduledQuotaLimits(
            daily=3, monthly=4, product=5, vendor=6, board=7
        ),
    )
    assert assessment.can_reserve is True
    assert assessment.already_reserved is False
    assert (
        assessment.daily_remaining,
        assessment.monthly_remaining,
        assessment.product_remaining,
        assessment.vendor_remaining,
        assessment.board_remaining,
    ) == (3, 4, 5, 6, 7)
    assert isolated_db.scalar(select(RoutineScheduledQuotaReservation)) is None


def test_read_only_assessment_fails_closed_when_any_quota_is_full(isolated_db):
    reserve_scheduled_quota(
        _PostgresReservationSession(isolated_db),
        publication_id="pub-booked",
        plan_id="plan-1",
        plan_item_id="item-booked",
        product_id="product-1",
        vendor_key="Acme",
        board_id="board-1",
        scheduled_for=date(2026, 4, 10),
        limits=ScheduledQuotaLimits(
            daily=10, monthly=10, product=10, vendor=10, board=10
        ),
    )
    isolated_db.commit()
    limits = ScheduledQuotaLimits(
        daily=1, monthly=1, product=1, vendor=1, board=1
    )
    assessment = assess_scheduled_quota(
        isolated_db,
        publication_id="pub-next",
        plan_id="plan-2",
        plan_item_id="item-next",
        product_id="product-1",
        vendor_key="acme",
        board_id="board-1",
        scheduled_for=date(2026, 4, 10),
        limits=limits,
    )
    assert assessment.can_reserve is False
    assert (
        assessment.daily_used,
        assessment.monthly_used,
        assessment.product_used,
        assessment.vendor_used,
        assessment.board_used,
    ) == (1, 1, 1, 1, 1)
    assert isolated_db.scalar(
        select(RoutineScheduledQuotaReservation).where(
            RoutineScheduledQuotaReservation.publication_id == "pub-next"
        )
    ) is None


def test_read_only_assessment_rejects_conflicting_publication_identity(isolated_db):
    db = _PostgresReservationSession(isolated_db)
    _reserve(db, publication="pub-existing")
    isolated_db.commit()
    with pytest.raises(ScheduledQuotaError) as error:
        assess_scheduled_quota(
            isolated_db,
            publication_id="pub-existing",
            plan_id="different-plan",
            plan_item_id="item-pub-existing",
            product_id="product-1",
            vendor_key="Acme",
            board_id="board-1",
            scheduled_for=date(2026, 4, 10),
            limits=ScheduledQuotaLimits(
                daily=10, monthly=10, product=10, vendor=10, board=10
            ),
        )
    assert error.value.code == "SCHEDULED_QUOTA_PUBLICATION_IDENTITY_CONFLICT"


def test_read_only_assessment_fails_closed_for_missing_ledger_schema():
    engine = create_engine("sqlite://")
    RoutinePublishingControl.__table__.create(engine)
    db = Session(engine)
    with pytest.raises(ScheduledQuotaError) as error:
        assess_scheduled_quota(
            db,
            publication_id="pub-1",
            plan_id="plan-1",
            plan_item_id="item-1",
            product_id="product-1",
            vendor_key="Acme",
            board_id="board-1",
            scheduled_for=date(2026, 4, 10),
            limits=ScheduledQuotaLimits(
                daily=1, monthly=1, product=1, vendor=1, board=1
            ),
        )
    assert error.value.code == "SCHEDULED_QUOTA_LEDGER_UNAVAILABLE"
    db.close()
    engine.dispose()


def test_read_only_assessment_rejects_invalid_input_before_query(isolated_db):
    with pytest.raises(ScheduledQuotaError) as error:
        assess_scheduled_quota(
            isolated_db,
            publication_id="pub-1",
            plan_id="plan-1",
            plan_item_id="item-1",
            product_id="product-1",
            vendor_key="Acme",
            board_id="board-1",
            scheduled_for="2026-04-10",
            limits=ScheduledQuotaLimits(
                daily=1, monthly=1, product=1, vendor=1, board=1
            ),
        )
    assert error.value.code == "SCHEDULED_QUOTA_DATE_REQUIRED"


def test_read_only_assessment_does_not_autoflush_pending_ledger_rows(isolated_db):
    pending = RoutineScheduledQuotaReservation(
        publication_id="uncommitted",
        plan_id="plan-1",
        plan_item_id="item-1",
        product_id="product-1",
        vendor_key="acme",
        board_id="board-1",
        scheduled_for=date(2026, 4, 10),
        month_start=date(2026, 4, 1),
    )
    isolated_db.add(pending)
    assessment = assess_scheduled_quota(
        isolated_db,
        publication_id="pub-assessment",
        plan_id="plan-1",
        plan_item_id="item-assessment",
        product_id="product-1",
        vendor_key="Acme",
        board_id="board-1",
        scheduled_for=date(2026, 4, 10),
        limits=ScheduledQuotaLimits(
            daily=2, monthly=2, product=2, vendor=2, board=2
        ),
    )
    assert assessment.daily_used == 0
    assert pending in isolated_db.new