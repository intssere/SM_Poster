"""Real disposable PostgreSQL concurrency contracts for scheduled admission."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

# These fixtures create only an isolated schema in a disposable Unix-socket
# PostgreSQL cluster, and seed canonical publication/plan lineage.
from test_routine_scheduled_commitments_postgres import (  # noqa: F401
    _seed_canonical_graph,
    isolated_postgres,
    local_postgres_url,
)
from app.core.config import Settings
from app.models.domain import (
    PinPublication,
    PublicationStatus,
    PinterestPortfolioPlan,
    PinterestPortfolioPlanItem,
)
from app.models.routine_publishing import (
    RoutineScheduledQuotaReservation,
)
from app.services.routine_scheduled_admission import admit_scheduled_publication
from app.services.routine_scheduled_commitments import (
    assess_scheduled_quota_with_commitments,
)
from app.services.routine_scheduled_quotas import ScheduledQuotaError
from app.services.routine_scheduled_quotas import ScheduledQuotaLimits


ADMISSION_NOW = datetime(2026, 4, 20, 12, tzinfo=timezone.utc)


def _settings(
    *,
    daily: int = 20,
    monthly: int = 20,
    product: int = 20,
    vendor_share: float = 1.0,
    board_share: float = 1.0,
) -> Settings:
    # database_url is required by Settings but admission must never use it.
    return Settings(
        database_url="postgresql+psycopg://unused.invalid/unused",
        routine_pinterest_daily_write_limit=daily,
        pinterest_monthly_pin_target=monthly,
        pinterest_portfolio_max_pins_per_product=product,
        pinterest_portfolio_max_vendor_share=vendor_share,
        pinterest_portfolio_max_board_share=board_share,
    )


def _admit(db: Session, identity: dict, settings: Settings):
    return admit_scheduled_publication(
        db,
        publication_id=identity["publication_id"],
        plan_item_id=identity["plan_item_id"],
        settings=settings,
        now=ADMISSION_NOW,
    )


def _snapshot(db: Session) -> tuple[list, list]:
    publications = db.execute(
        sa.select(
            PinPublication.id,
            PinPublication.status,
            PinPublication.scheduled_for,
            PinPublication.attempt_started_at,
        ).order_by(PinPublication.id)
    ).all()
    ledger = db.execute(
        sa.select(
            RoutineScheduledQuotaReservation.id,
            RoutineScheduledQuotaReservation.publication_id,
            RoutineScheduledQuotaReservation.plan_id,
            RoutineScheduledQuotaReservation.plan_item_id,
            RoutineScheduledQuotaReservation.product_id,
            RoutineScheduledQuotaReservation.vendor_key,
            RoutineScheduledQuotaReservation.board_id,
            RoutineScheduledQuotaReservation.scheduled_for,
            RoutineScheduledQuotaReservation.month_start,
        ).order_by(RoutineScheduledQuotaReservation.id)
    ).all()
    return publications, ledger


def _set_timeouts(db: Session) -> None:
    db.execute(sa.text("SET LOCAL lock_timeout = '8s'"))
    db.execute(sa.text("SET LOCAL statement_timeout = '15s'"))


def _wait_for_control_row_lock(engine: sa.Engine, pid: int, future) -> bool:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        with engine.connect() as monitor:
            wait_type = monitor.execute(
                sa.text(
                    "SELECT wait_event_type FROM pg_stat_activity "
                    "WHERE pid = :pid AND query ILIKE '%routine_publishing_control%'"
                ),
                {"pid": pid},
            ).scalar_one_or_none()
        if wait_type == "Lock":
            return True
        if future.done():
            return False
        time.sleep(0.025)
    return False


def _dimension_publications(dimension: str) -> list[dict]:
    first = {
        "label": f"admission-{dimension}-first",
        "day": date(2026, 4, 10),
        "product_id": f"product-{dimension}-first",
        "vendor": f"Vendor {dimension} first",
        "board_id": f"board-{dimension}-first",
    }
    second = {
        "label": f"admission-{dimension}-second",
        "day": date(2026, 4, 11),
        "product_id": f"product-{dimension}-second",
        "vendor": f"Vendor {dimension} second",
        "board_id": f"board-{dimension}-second",
    }
    if dimension == "daily":
        second["day"] = first["day"]
    elif dimension == "product":
        second["product_id"] = first["product_id"]
    elif dimension == "vendor":
        second["vendor"] = first["vendor"]
    elif dimension == "board":
        second["board_id"] = first["board_id"]
    return [first, second]


@pytest.mark.parametrize("dimension", ["daily", "monthly", "product", "vendor", "board"])
def test_postgres_admission_serializes_overcommitted_persisted_commitments_and_rejects_both(
    isolated_postgres, dimension
):
    engine, sessions, stub_tables = isolated_postgres
    specs = _dimension_publications(dimension)
    identities = _seed_canonical_graph(engine, stub_tables, publications=specs)

    kwargs = {
        "daily": 1 if dimension == "daily" else 20,
        "monthly": 20,
        "product": 1 if dimension == "product" else 20,
        "vendor_share": 0.05 if dimension == "vendor" else 1.0,
        "board_share": 0.05 if dimension == "board" else 1.0,
    }
    if dimension == "monthly":
        # Month target is the monthly limit; each publication otherwise has a
        # distinct product, vendor, board, and scheduled day.
        with engine.begin() as connection:
            connection.execute(
                sa.update(PinterestPortfolioPlan)
                .where(PinterestPortfolioPlan.id == "plan-commitments")
                .values(target_pins=1)
            )
        kwargs["monthly"] = 1
    settings = _settings(**kwargs)
    # All five persisted SCHEDULED rows already count, even before either call
    # has a ledger row. Only the chosen dimension is saturated.
    first, second = (identities[spec["label"]] for spec in specs)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    errors: list[str] = []
    first_locked = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    second_pid: list[int] = []

    def reject_and_hold(identity: dict, *, hold: bool):
        with sessions() as db:
            _set_timeouts(db)
            if not hold:
                second_pid.append(
                    int(db.connection().exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
                )
                second_started.set()
            try:
                _admit(db, identity, settings)
                result = "admitted"
            except ScheduledQuotaError as exc:
                result = exc.code
            if hold:
                first_locked.set()
                assert release_first.wait(timeout=10), "timed out releasing first admission transaction"
            else:
                # Keep the competing session's transaction alive until the
                # test releases the first control-row lock.
                assert release_first.wait(timeout=10), "timed out releasing competing admission transaction"
            db.rollback()
            return result

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(reject_and_hold, first, hold=True)
            assert first_locked.wait(timeout=10), "first admission did not reach its transaction"
            second_future = pool.submit(reject_and_hold, second, hold=False)
            assert second_started.wait(timeout=10), "second admission did not start"
            assert _wait_for_control_row_lock(engine, second_pid[0], second_future), (
                "competing admission did not wait on PostgreSQL's control-row lock"
            )
            release_first.set()
            results = [first_future.result(timeout=15), second_future.result(timeout=15)]
    finally:
        release_first.set()

    assert results == ["SCHEDULED_QUOTA_EXISTING_LIMIT_EXCEEDED"] * 2
    with sessions() as db:
        assert _snapshot(db) == (
            [
                (
                    f"publication-{spec['label']}",
                    PublicationStatus.SCHEDULED,
                    datetime.combine(spec["day"], datetime.min.time(), tzinfo=timezone.utc),
                    None,
                )
                for spec in sorted(specs, key=lambda item: f"publication-{item['label']}")
            ],
            [],
        )


def test_postgres_at_capacity_serialized_admissions_commit_without_oversubscription(
    isolated_postgres,
):
    engine, sessions, stub_tables = isolated_postgres
    specs = [
        {
            "label": "at-capacity-one",
            "day": date(2026, 4, 10),
            "product_id": "at-capacity-product",
            "vendor": "At Capacity",
            "board_id": "at-capacity-board",
        },
        {
            "label": "at-capacity-two",
            "day": date(2026, 4, 10),
            "product_id": "at-capacity-product",
            "vendor": "At Capacity",
            "board_id": "at-capacity-board",
        },
    ]
    identities = _seed_canonical_graph(engine, stub_tables, publications=specs)
    settings = _settings(
        daily=2, product=2, vendor_share=1.0, board_share=1.0
    )
    with engine.begin() as connection:
        connection.execute(
            sa.update(PinterestPortfolioPlan)
            .where(PinterestPortfolioPlan.id == "plan-commitments")
            .values(target_pins=2)
        )
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    first, second = (identities[spec["label"]] for spec in specs)
    first_admitted = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    second_pid: list[int] = []

    def admit_and_hold(identity: dict, *, hold: bool):
        with sessions() as db:
            _set_timeouts(db)
            if not hold:
                second_pid.append(
                    int(db.connection().exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
                )
                second_started.set()
            admission = _admit(db, identity, settings)
            if hold:
                first_admitted.set()
                assert release_first.wait(timeout=10), "timed out releasing first successful admission"
            else:
                assert release_first.wait(timeout=10), "timed out releasing second successful admission"
            db.commit()
            return admission

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(admit_and_hold, first, hold=True)
            assert first_admitted.wait(timeout=10), "first admission did not reach its held transaction"
            second_future = pool.submit(admit_and_hold, second, hold=False)
            assert second_started.wait(timeout=10), "second admission did not start"
            assert _wait_for_control_row_lock(engine, second_pid[0], second_future), (
                "second admission did not wait on PostgreSQL's control-row lock"
            )
            release_first.set()
            admissions = [first_future.result(timeout=15), second_future.result(timeout=15)]
    finally:
        release_first.set()

    assert {admission.publication_id for admission in admissions} == {
        first["publication_id"],
        second["publication_id"],
    }
    with sessions() as db:
        assert db.scalar(
            sa.select(sa.func.count()).select_from(RoutineScheduledQuotaReservation)
        ) == 2
        assert db.scalar(
            sa.select(sa.func.count())
            .select_from(PinPublication)
            .where(PinPublication.status == PublicationStatus.PUBLISHING)
        ) == 2
        assessment = assess_scheduled_quota_with_commitments(
            db,
            **{
                **first,
                "limits": ScheduledQuotaLimits(2, 2, 2, 2, 2),
            },
        )
        assert (
            assessment.daily_used,
            assessment.monthly_used,
            assessment.product_used,
            assessment.vendor_used,
            assessment.board_used,
        ) == (2, 2, 2, 2, 2)


def test_postgres_duplicate_admission_claims_once_then_rejects_without_mutation(
    isolated_postgres,
):
    engine, sessions, stub_tables = isolated_postgres
    identities = _seed_canonical_graph(
        engine,
        stub_tables,
        publications=[
            {
                "label": "duplicate-admission",
                "day": date(2026, 4, 10),
                "product_id": "duplicate-product",
                "vendor": "Duplicate Vendor",
                "board_id": "duplicate-board",
            }
        ],
    )
    identity = identities["duplicate-admission"]
    settings = _settings()
    with sessions() as db:
        admission = _admit(db, identity, settings)
        assert admission.publication_id == identity["publication_id"]
        db.commit()
    with sessions() as db:
        before = _snapshot(db)
        with pytest.raises(ScheduledQuotaError) as error:
            _admit(db, identity, settings)
        assert error.value.code == "SCHEDULED_QUOTA_PUBLICATION_NOT_CLAIMABLE"
        db.rollback()
        assert _snapshot(db) == before
        assert len(before[1]) == 1
        assert before[0][0][1] == PublicationStatus.PUBLISHING


def test_postgres_commitment_and_ledger_overlap_counts_once_during_admission(
    isolated_postgres,
):
    engine, sessions, stub_tables = isolated_postgres
    specs = [
        {
            "label": "overlap-target",
            "day": date(2026, 4, 10),
            "product_id": "overlap-product",
            "vendor": "Overlap Vendor",
            "board_id": "overlap-board",
        },
        {
            "label": "overlap-peer",
            "day": date(2026, 4, 11),
            "product_id": "overlap-product",
            "vendor": "Overlap Vendor",
            "board_id": "overlap-board",
        },
    ]
    identities = _seed_canonical_graph(engine, stub_tables, publications=specs)
    identity = identities["overlap-target"]
    settings = _settings()
    with sessions() as db:
        admission = _admit(db, identity, settings)
        assert admission.used == (1, 2, 2, 2, 2)
        before_commit = assess_scheduled_quota_with_commitments(
            db,
            **{
                **identity,
                "limits": ScheduledQuotaLimits(20, 20, 20, 20, 20),
            },
        )
        assert (
            before_commit.daily_used,
            before_commit.monthly_used,
            before_commit.product_used,
            before_commit.vendor_used,
            before_commit.board_used,
        ) == admission.used
        db.commit()
    with sessions() as db:
        assert db.scalar(
            sa.select(sa.func.count()).select_from(RoutineScheduledQuotaReservation)
        ) == 1
        assert db.get(PinPublication, identity["publication_id"]).status == PublicationStatus.PUBLISHING


@pytest.mark.parametrize("release_mode", ["rollback", "connection_loss"])
def test_postgres_admission_rollback_or_connection_loss_releases_claim_and_ledger(
    isolated_postgres, release_mode
):
    engine, sessions, stub_tables = isolated_postgres
    identities = _seed_canonical_graph(
        engine,
        stub_tables,
        publications=[
            {
                "label": f"release-{release_mode}",
                "day": date(2026, 4, 10),
                "product_id": f"release-product-{release_mode}",
                "vendor": "Release Vendor",
                "board_id": f"release-board-{release_mode}",
            }
        ],
    )
    identity = identities[f"release-{release_mode}"]
    abandoned = sessions()
    try:
        _admit(abandoned, identity, _settings())
        if release_mode == "rollback":
            abandoned.rollback()
        else:
            abandoned.connection().invalidate()
            abandoned.close()
        with sessions() as db:
            assert _snapshot(db)[1] == []
            assert db.get(PinPublication, identity["publication_id"]).status == PublicationStatus.SCHEDULED
            admission = _admit(db, identity, _settings())
            assert admission.publication_id == identity["publication_id"]
            db.commit()
        with sessions() as db:
            assert db.scalar(
                sa.select(sa.func.count()).select_from(RoutineScheduledQuotaReservation)
            ) == 1
            assert db.get(PinPublication, identity["publication_id"]).status == PublicationStatus.PUBLISHING
    finally:
        abandoned.close()


@pytest.mark.parametrize(
    ("drift", "expected"),
    [
        ("board_lineage", "SCHEDULED_QUOTA_COMMITMENT_CONFLICT"),
        ("stale_month", "SCHEDULED_QUOTA_PLAN_ENVELOPE_INVALID"),
    ],
)
def test_postgres_admission_rejects_stale_or_conflicting_lineage_without_mutation(
    isolated_postgres, drift, expected
):
    engine, sessions, stub_tables = isolated_postgres
    identities = _seed_canonical_graph(
        engine,
        stub_tables,
        publications=[
            {
                "label": f"lineage-{drift}",
                "day": date(2026, 4, 10),
                "product_id": f"lineage-product-{drift}",
                "vendor": "Lineage Vendor",
                "board_id": f"lineage-board-{drift}",
            }
        ],
    )
    identity = identities[f"lineage-{drift}"]
    with engine.begin() as connection:
        if drift == "board_lineage":
            connection.execute(
                sa.update(PinterestPortfolioPlanItem)
                .where(PinterestPortfolioPlanItem.id == identity["plan_item_id"])
                .values(board_key_snapshot="drifted-board-key")
            )
        else:
            moved = datetime(2026, 3, 10, tzinfo=timezone.utc)
            connection.execute(
                sa.update(PinPublication)
                .where(PinPublication.id == identity["publication_id"])
                .values(scheduled_for=moved)
            )
            connection.execute(
                sa.update(PinterestPortfolioPlanItem)
                .where(PinterestPortfolioPlanItem.id == identity["plan_item_id"])
                .values(planned_date=date(2026, 3, 10))
            )
    with sessions() as db:
        before = _snapshot(db)
        with pytest.raises(ScheduledQuotaError) as error:
            _admit(db, identity, _settings())
        assert error.value.code == expected
        db.rollback()
        assert _snapshot(db) == before


def test_postgres_admission_cas_and_ledger_are_visible_only_after_same_commit(
    isolated_postgres,
):
    engine, sessions, stub_tables = isolated_postgres
    identities = _seed_canonical_graph(
        engine,
        stub_tables,
        publications=[
            {
                "label": "atomic-admission",
                "day": date(2026, 4, 10),
                "product_id": "atomic-product",
                "vendor": "Atomic Vendor",
                "board_id": "atomic-board",
            }
        ],
    )
    identity = identities["atomic-admission"]
    db = sessions()
    try:
        admission = _admit(db, identity, _settings())
        assert admission.reservation_id
        assert db.get(PinPublication, identity["publication_id"]).status == PublicationStatus.PUBLISHING
        assert db.scalar(
            sa.select(sa.func.count()).select_from(RoutineScheduledQuotaReservation)
        ) == 1
        with sessions() as observer:
            assert observer.get(PinPublication, identity["publication_id"]).status == PublicationStatus.SCHEDULED
            assert observer.scalar(
                sa.select(sa.func.count()).select_from(RoutineScheduledQuotaReservation)
            ) == 0
        db.commit()
    finally:
        db.close()
    with sessions() as observer:
        assert observer.get(PinPublication, identity["publication_id"]).status == PublicationStatus.PUBLISHING
        assert observer.scalar(
            sa.select(sa.func.count()).select_from(RoutineScheduledQuotaReservation)
        ) == 1