from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models.domain import (
    Board,
    ContentAngle,
    PinConcept,
    PinDraft,
    PinPublication,
    Product,
    PublicationStatus,
    PinterestPortfolioPlan,
    PinterestPortfolioPlanItem,
    Store,
)
from app.models.routine_publishing import RoutineScheduledQuotaReservation
from app.services.routine_scheduled_commitments import (
    assess_scheduled_quota_with_commitments,
)
from app.services.routine_scheduled_quotas import (
    ScheduledQuotaError,
    ScheduledQuotaLimits,
)


DAY = date(2026, 4, 10)
MONTH_START = date(2026, 4, 1)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine)
    yield session
    session.close()
    engine.dispose()


def _seed_catalog(db):
    store = Store(id="store-1", name="Store", shop_domain="store.example")
    angle = ContentAngle(id="angle-1", key="angle-1", name="Angle")
    products = [
        Product(
            id="product-1",
            store_id=store.id,
            shopify_product_id="shopify-1",
            handle="product-1",
            title="Product 1",
            vendor="Acme",
            product_url="https://example.test/product-1",
        ),
        Product(
            id="product-2",
            store_id=store.id,
            shopify_product_id="shopify-2",
            handle="product-2",
            title="Product 2",
            vendor="Acme",
            product_url="https://example.test/product-2",
        ),
    ]
    boards = [
        Board(id="board-1", store_id=store.id, name="Board 1", slug="board-1"),
        Board(id="board-2", store_id=store.id, name="Board 2", slug="board-2"),
    ]
    plan = PinterestPortfolioPlan(
        id="plan-1",
        store_id=store.id,
        month_start=MONTH_START,
        month_end=date(2026, 4, 30),
        target_pins=10,
        existing_commitments=0,
        planned_active_slots=10,
        reserve_slots=0,
        policy_version="test-v1",
        input_fingerprint="i" * 64,
        plan_fingerprint="p" * 64,
        status="ACTIVE",
        metadata_json={},
    )
    db.add_all([store, angle, *products, *boards, plan])
    db.flush()
    return store, angle, products, boards, plan


def _add_commitment(
    db,
    *,
    suffix,
    store,
    angle,
    products,
    boards,
    plan,
    scheduled_for=DAY,
    product_index=0,
    board_index=0,
    status=PublicationStatus.SCHEDULED,
    plan_item_status="SCHEDULED",
    plan_status=None,
    item_publication_id=None,
    reserve=False,
):
    product = products[product_index]
    board = boards[board_index]
    publication_id = f"publication-{suffix}"
    concept = PinConcept(
        id=f"concept-{suffix}",
        store_id=store.id,
        product_id=product.id,
        content_angle_id=angle.id,
        board_id=board.id,
        fingerprint=f"{suffix:0>64}"[-64:],
        rationale={},
    )
    draft = PinDraft(
        id=f"draft-{suffix}",
        concept_id=concept.id,
        version=1,
        title="Pin title",
        description="Pin description",
        alt_text="Pin alt text",
        destination_url="https://example.test",
        utm_url="https://example.test?utm_source=pinterest",
        text_fingerprint=f"{'d' + str(suffix):0>64}"[-64:],
    )
    publication = PinPublication(
        id=publication_id,
        draft_id=draft.id,
        # Provider/creative lineage is intentionally outside quota identity.
        creative_id=f"creative-{suffix}",
        board_id=board.id,
        publication_fingerprint=f"{'f' + str(suffix):0>64}"[-64:],
        status=status,
        scheduled_for=datetime.combine(scheduled_for, datetime.min.time(), tzinfo=timezone.utc),
    )
    item = PinterestPortfolioPlanItem(
        id=f"item-{suffix}",
        plan_id=plan.id,
        slot_index=int(suffix),
        is_reserve=reserve,
        planned_date=scheduled_for,
        product_id=product.id,
        local_board_id=board.id,
        board_key_snapshot=board.slug,
        content_angle_id=angle.id,
        angle_key_snapshot=angle.key,
        seed_keywords=[],
        selection_score=1,
        selection_metadata={},
        item_fingerprint=f"{'x' + str(suffix):0>64}"[-64:],
        status=plan_item_status,
        publication_id=item_publication_id or publication.id,
    )
    db.add_all([concept, draft, publication, item])
    db.flush()
    if plan_status is not None:
        plan.status = plan_status
    return publication, item


def _seed_target(db, **kwargs):
    store, angle, products, boards, plan = _seed_catalog(db)
    publication, item = _add_commitment(
        db,
        suffix="1",
        store=store,
        angle=angle,
        products=products,
        boards=boards,
        plan=plan,
        **kwargs,
    )
    db.commit()
    return {
        "publication_id": publication.id,
        "plan_id": plan.id,
        "plan_item_id": item.id,
        "product_id": products[kwargs.get("product_index", 0)].id,
        "vendor_key": products[kwargs.get("product_index", 0)].vendor,
        "board_id": boards[kwargs.get("board_index", 0)].id,
        "scheduled_for": kwargs.get("scheduled_for", DAY),
        "store": store,
        "angle": angle,
        "products": products,
        "boards": boards,
        "plan": plan,
        "publication": publication,
        "item": item,
    }


def _assess(db, identity, limits=None):
    values = {
        key: identity[key]
        for key in (
            "publication_id",
            "plan_id",
            "plan_item_id",
            "product_id",
            "vendor_key",
            "board_id",
            "scheduled_for",
        )
    }
    return assess_scheduled_quota_with_commitments(
        db,
        **values,
        limits=limits or ScheduledQuotaLimits(
            daily=10, monthly=10, product=10, vendor=10, board=10
        ),
    )


@pytest.mark.parametrize(
    ("quota_dimension", "other_product", "other_board", "other_day"),
    [
        ("daily", 1, 1, DAY),
        ("monthly", 1, 1, date(2026, 4, 11)),
        ("product", 0, 1, date(2026, 4, 11)),
        ("vendor", 1, 1, date(2026, 4, 11)),
        ("board", 1, 0, date(2026, 4, 11)),
    ],
)
def test_unreserved_commitments_count_in_each_quota_dimension(
    db, quota_dimension, other_product, other_board, other_day
):
    identity = _seed_target(db)
    _add_commitment(
        db,
        suffix="2",
        store=identity["store"],
        angle=identity["angle"],
        products=identity["products"],
        boards=identity["boards"],
        plan=identity["plan"],
        product_index=other_product,
        board_index=other_board,
        scheduled_for=other_day,
    )
    db.commit()
    limits = ScheduledQuotaLimits(
        **{name: 2 if name == quota_dimension else 20
           for name in ("daily", "monthly", "product", "vendor", "board")}
    )

    assessment = _assess(db, identity, limits)

    assert getattr(assessment, f"{quota_dimension}_used") == 2
    assert getattr(assessment, f"{quota_dimension}_remaining") == 0
    assert assessment.can_reserve is False
    assert assessment.already_committed is True


def test_ledger_overlap_is_counted_once_with_unreserved_commitments(db):
    identity = _seed_target(db)
    other, other_item = _add_commitment(
        db,
        suffix="2",
        store=identity["store"],
        angle=identity["angle"],
        products=identity["products"],
        boards=identity["boards"],
        plan=identity["plan"],
    )
    db.add(
        RoutineScheduledQuotaReservation(
            publication_id=other.id,
            plan_id=identity["plan"].id,
            plan_item_id=other_item.id,
            product_id=identity["products"][0].id,
            vendor_key="acme",
            board_id=identity["boards"][0].id,
            scheduled_for=DAY,
            month_start=MONTH_START,
        )
    )
    db.commit()

    assessment = _assess(db, identity)

    assert (assessment.daily_used, assessment.monthly_used) == (2, 2)
    assert assessment.already_reserved is False
    assert db.scalar(select(func.count()).select_from(RoutineScheduledQuotaReservation)) == 1


def test_current_publication_is_idempotently_committed_with_ledger(db):
    identity = _seed_target(db)
    db.add(
        RoutineScheduledQuotaReservation(
            publication_id=identity["publication_id"],
            plan_id=identity["plan_id"],
            plan_item_id=identity["plan_item_id"],
            product_id=identity["product_id"],
            vendor_key="acme",
            board_id=identity["board_id"],
            scheduled_for=DAY,
            month_start=MONTH_START,
        )
    )
    db.commit()

    assessment = _assess(db, identity)

    assert assessment.already_reserved is True
    assert assessment.already_committed is True
    assert (assessment.daily_used, assessment.monthly_used) == (1, 1)


def test_current_publication_at_existing_limit_is_valid_but_shrink_fails(db):
    identity = _seed_target(db)
    _add_commitment(
        db,
        suffix="2",
        store=identity["store"],
        angle=identity["angle"],
        products=identity["products"],
        boards=identity["boards"],
        plan=identity["plan"],
    )
    db.commit()
    equal_limits = ScheduledQuotaLimits(daily=2, monthly=2, product=2, vendor=2, board=2)

    assessment = _assess(db, identity, equal_limits)

    assert assessment.already_committed is True
    assert assessment.can_reserve is False
    assert (
        assessment.daily_used,
        assessment.monthly_used,
        assessment.product_used,
        assessment.vendor_used,
        assessment.board_used,
    ) == (2, 2, 2, 2, 2)
    with pytest.raises(ScheduledQuotaError) as error:
        _assess(
            db,
            identity,
            ScheduledQuotaLimits(daily=1, monthly=2, product=2, vendor=2, board=2),
        )
    assert error.value.code == "SCHEDULED_QUOTA_EXISTING_LIMIT_EXCEEDED"


def test_caller_identity_conflict_and_duplicate_plan_item_are_rejected(db):
    identity = _seed_target(db)
    wrong_identity = {**identity, "plan_item_id": "another-item"}
    with pytest.raises(ScheduledQuotaError) as error:
        _assess(db, wrong_identity)
    assert error.value.code == "SCHEDULED_QUOTA_COMMITMENT_CONFLICT"

    duplicate = PinterestPortfolioPlanItem(
        id="item-duplicate",
        plan_id=identity["plan_id"],
        slot_index=2,
        is_reserve=False,
        planned_date=DAY,
        product_id=identity["product_id"],
        local_board_id=identity["board_id"],
        board_key_snapshot="board-1",
        content_angle_id=identity["angle"].id,
        angle_key_snapshot=identity["angle"].key,
        seed_keywords=[],
        selection_score=1,
        selection_metadata={},
        item_fingerprint="z" * 64,
        status="SCHEDULED",
        publication_id=identity["publication_id"],
    )
    db.add(duplicate)
    db.commit()
    with pytest.raises(ScheduledQuotaError) as error:
        _assess(db, identity)
    assert error.value.code == "SCHEDULED_QUOTA_COMMITMENT_AMBIGUOUS"


@pytest.mark.parametrize(
    ("corruption", "expected_code"),
    [
        ("schedule", "SCHEDULED_QUOTA_COMMITMENT_STALE"),
        ("vendor", "SCHEDULED_QUOTA_COMMITMENT_CONFLICT"),
        ("board", "SCHEDULED_QUOTA_COMMITMENT_CONFLICT"),
    ],
)
def test_stale_schedule_vendor_and_board_lineage_fail_closed(db, corruption, expected_code):
    identity = _seed_target(db)
    if corruption == "schedule":
        identity["publication"].scheduled_for = datetime(
            2026, 5, 10, tzinfo=timezone.utc
        )
    elif corruption == "vendor":
        identity["products"][0].vendor = "Changed vendor"
    else:
        identity["boards"][0].slug = "changed-board"
    db.commit()

    with pytest.raises(ScheduledQuotaError) as error:
        _assess(db, identity)
    assert error.value.code == expected_code


def test_cancelled_publication_with_ledger_is_stale(db):
    identity = _seed_target(db)
    cancelled, item = _add_commitment(
        db,
        suffix="2",
        store=identity["store"],
        angle=identity["angle"],
        products=identity["products"],
        boards=identity["boards"],
        plan=identity["plan"],
        status=PublicationStatus.CANCELLED,
        plan_item_status="SCHEDULED",
    )
    db.add(
        RoutineScheduledQuotaReservation(
            publication_id=cancelled.id,
            plan_id=identity["plan_id"],
            plan_item_id=item.id,
            product_id=identity["products"][0].id,
            vendor_key="acme",
            board_id=identity["boards"][0].id,
            scheduled_for=DAY,
            month_start=MONTH_START,
        )
    )
    db.commit()
    with pytest.raises(ScheduledQuotaError) as error:
        _assess(db, identity)
    assert error.value.code == "SCHEDULED_QUOTA_COMMITMENT_STALE"


def test_cancelled_unreserved_publication_does_not_add_quota(db):
    identity = _seed_target(db)
    cancelled, _ = _add_commitment(
        db,
        suffix="2",
        store=identity["store"],
        angle=identity["angle"],
        products=identity["products"],
        boards=identity["boards"],
        plan=identity["plan"],
        status=PublicationStatus.CANCELLED,
        plan_item_status="SCHEDULED",
    )
    db.commit()
    assessment = _assess(db, identity)
    assert assessment.monthly_used == 1
    assert cancelled.status == PublicationStatus.CANCELLED


def test_assessment_is_read_only_and_does_not_autoflush_pending_ledger(db):
    identity = _seed_target(db)
    pending = RoutineScheduledQuotaReservation(
        publication_id="pending-publication",
        plan_id=identity["plan_id"],
        plan_item_id=identity["plan_item_id"],
        product_id=identity["product_id"],
        vendor_key="acme",
        board_id=identity["board_id"],
        scheduled_for=DAY,
        month_start=MONTH_START,
    )
    db.add(pending)

    assessment = _assess(db, identity)

    assert assessment.monthly_used == 1
    assert pending in db.new
    with db.no_autoflush:
        assert db.scalar(
            select(func.count()).select_from(RoutineScheduledQuotaReservation)
        ) == 0


@pytest.mark.parametrize(
    "new_scheduled_for",
    [
        datetime(2026, 5, 10, tzinfo=timezone.utc),
        None,
    ],
    ids=("moved-to-next-month", "schedule-cleared"),
)
def test_current_month_plan_item_with_moved_or_missing_publication_schedule_fails(
    db, new_scheduled_for
):
    identity = _seed_target(db)
    identity["publication"].scheduled_for = new_scheduled_for
    db.commit()

    with pytest.raises(ScheduledQuotaError) as error:
        _assess(db, identity)

    assert error.value.code == "SCHEDULED_QUOTA_COMMITMENT_STALE"
    assert db.scalar(
        select(func.count()).select_from(RoutineScheduledQuotaReservation)
    ) == 0


def test_discovered_current_month_publication_with_next_month_ledger_fails(db):
    identity = _seed_target(db)
    other, other_item = _add_commitment(
        db,
        suffix="2",
        store=identity["store"],
        angle=identity["angle"],
        products=identity["products"],
        boards=identity["boards"],
        plan=identity["plan"],
    )
    db.add(
        RoutineScheduledQuotaReservation(
            publication_id=other.id,
            plan_id=identity["plan_id"],
            plan_item_id=other_item.id,
            product_id=identity["products"][0].id,
            vendor_key="acme",
            board_id=identity["boards"][0].id,
            scheduled_for=date(2026, 5, 10),
            month_start=date(2026, 5, 1),
        )
    )
    db.commit()

    with pytest.raises(ScheduledQuotaError) as error:
        _assess(db, identity)

    assert error.value.code == "SCHEDULED_QUOTA_COMMITMENT_CONFLICT"


@pytest.mark.parametrize(
    "new_scheduled_for",
    [
        datetime(2026, 5, 10, tzinfo=timezone.utc),
        None,
    ],
    ids=("moved-to-next-month", "schedule-cleared"),
)
def test_stale_other_publication_in_current_month_plan_blocks_candidate(
    db, new_scheduled_for
):
    identity = _seed_target(db)
    other, _ = _add_commitment(
        db,
        suffix="2",
        store=identity["store"],
        angle=identity["angle"],
        products=identity["products"],
        boards=identity["boards"],
        plan=identity["plan"],
    )
    other.scheduled_for = new_scheduled_for
    db.commit()

    with pytest.raises(ScheduledQuotaError) as error:
        _assess(db, identity)

    assert error.value.code == "SCHEDULED_QUOTA_COMMITMENT_STALE"
