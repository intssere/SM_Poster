from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.models.domain import (
    Board,
    ContentAngle,
    PinConcept,
    PinDraft,
    PinPublication,
    PinterestPortfolioPlan,
    PinterestPortfolioSlot,
    Product,
    ProductIntelligence,
    PublicationStatus,
    Store,
)
from app.services import pinterest_portfolio_planner as planner


UTC = timezone.utc
NOW = datetime(2026, 9, 20, 18, 0, tzinfo=UTC)


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _settings(**overrides):
    values = {
        "database_url": "sqlite:///:memory:",
        "pinterest_portfolio_planning_enabled": False,
        "pinterest_monthly_pin_target": 150,
        "pinterest_portfolio_timezone": "America/Chicago",
        "pinterest_portfolio_window_start_hour": 8,
        "pinterest_portfolio_window_end_hour": 22,
        "pinterest_product_monthly_cap": 3,
        "pinterest_portfolio_reserve_percentage": 0.10,
    }
    values.update(overrides)
    return Settings(**values)


def _seed_taxonomy(db, *, include_guide_board=True, include_under50=True):
    store = Store(id="store-1", name="Diamond Shelf", shop_domain="diamondshelf.us", market="US")
    db.add(store)
    db.add(Board(
        id="board-arabian",
        store_id=store.id,
        name="Arabian Fragrance",
        slug="arabian-fragrance",
        rules={},
        active=True,
    ))
    if include_guide_board:
        db.add(Board(
            id="board-guides",
            store_id=store.id,
            name="Fragrance Guides",
            slug="fragrance-guides",
            rules={},
            active=True,
        ))
    db.add(ContentAngle(
        id="angle-spotlight",
        key="product-spotlight",
        name="Product Spotlight",
        active=True,
        rules={},
    ))
    db.add(ContentAngle(
        id="angle-arabian",
        key="arabian-fragrance-discovery",
        name="Arabian Fragrance Discovery",
        active=True,
        rules={},
    ))
    if include_under50:
        db.add(ContentAngle(
            id="angle-under50",
            key="under-50",
            name="Fragrance Under $50",
            active=True,
            rules={},
        ))
    db.commit()
    return store


def _seed_products(db, count=60, *, store_id="store-1"):
    rows = []
    for index in range(count):
        product = Product(
            id=f"product-{index:03d}",
            store_id=store_id,
            shopify_product_id=f"shop-{index}",
            handle=f"product-{index}",
            title=f"Arabian Fragrance {index}",
            vendor=f"Brand {index % 10:02d}",
            product_type="Fragrance",
            status="ACTIVE",
            product_url=f"https://diamondshelf.us/products/product-{index}",
            tags=[],
            attributes={},
            collections=[],
            shopify_data={},
            inventory_total=10,
            price_min=39,
            compare_at_min=None,
            manual_priority=index % 4,
            excluded_from_editorial=False,
            shopify_created_at=datetime(2026, 8, 1, tzinfo=UTC),
        )
        intel = ProductIntelligence(
            id=f"intel-{index:03d}",
            product_id=product.id,
            brand=product.vendor,
            audience="unisex",
            designer=None,
            niche=None,
            arabian_classification="arabian",
            fragrance_family="Amber",
            fragrance_notes=["amber"],
            concentration="Eau de Parfum",
            size="100 ml",
            price_band="under_50",
            gift_suitability=None,
            season=None,
            occasion=None,
            image_quality=1,
            image_available=True,
            inventory_eligible=True,
            eligibility_score=90 + (index % 5),
            eligibility_status="ELIGIBLE",
            eligibility_reasons=[],
            normalization_status="COMPLETE",
            normalized_data={"normalization_category": "fragrance"},
        )
        db.add_all([product, intel])
        rows.append((product, intel))
    db.commit()
    return rows


def _seed_ineligible_product(db, *, store_id="store-1"):
    product = Product(
        id="product-ineligible",
        store_id=store_id,
        shopify_product_id="shop-ineligible",
        handle="ineligible",
        title="Ineligible",
        vendor="Blocked Brand",
        product_type="Fragrance",
        status="ACTIVE",
        product_url="https://diamondshelf.us/products/ineligible",
        tags=[],
        attributes={},
        collections=[],
        shopify_data={},
        inventory_total=10,
        price_min=30,
        manual_priority=100,
        excluded_from_editorial=False,
    )
    intel = ProductIntelligence(
        id="intel-ineligible",
        product_id=product.id,
        brand="Blocked Brand",
        image_quality=1,
        image_available=True,
        inventory_eligible=False,
        eligibility_score=100,
        eligibility_status="INELIGIBLE",
        eligibility_reasons=[],
        normalization_status="COMPLETE",
        normalized_data={"normalization_category": "fragrance"},
    )
    db.add_all([product, intel])
    db.commit()


def _existing_publication(db, product_id: str, scheduled_for: datetime, sequence: int):
    concept = PinConcept(
        id=f"existing-concept-{sequence}",
        store_id="store-1",
        product_id=product_id,
        content_angle_id="angle-spotlight",
        keyword_cluster_id=None,
        board_id="board-arabian",
        campaign_id=None,
        fingerprint=f"{sequence + 1:064x}"[-64:],
        rationale={},
    )
    draft = PinDraft(
        id=f"existing-draft-{sequence}",
        concept_id=concept.id,
        version=1,
        title="Existing",
        description="Existing",
        alt_text="Existing",
        destination_url="https://diamondshelf.us",
        utm_url="https://diamondshelf.us?utm_source=pinterest",
        text_fingerprint=f"{sequence + 101:064x}"[-64:],
    )
    publication = PinPublication(
        id=f"existing-publication-{sequence}",
        draft_id=draft.id,
        creative_id=f"creative-existing-{sequence}",
        approval_id=None,
        board_id="board-arabian",
        publication_fingerprint=f"{sequence + 201:064x}"[-64:],
        status=PublicationStatus.SCHEDULED,
        scheduled_for=scheduled_for,
    )
    db.add_all([concept, draft, publication])
    db.commit()
    return publication


def _ready_db(products=60):
    db = _db()
    _seed_taxonomy(db)
    _seed_products(db, products)
    return db


def test_150_pin_30_day_month_is_five_per_day():
    db = _ready_db()
    preview = planner.portfolio_preview(
        db,
        month_key="2026-11",
        settings=_settings(),
        now=NOW,
    )
    assert preview["ready"] is True
    assert preview["planned_count"] == 150
    assert preview["reserve_count"] == 15
    assert len(preview["daily_counts"]) == 30
    assert set(preview["daily_counts"].values()) == {5}
    assert sum(preview["daily_counts"].values()) == 150
    db.close()


def test_150_pin_31_day_month_evenly_distributes_four_and_five():
    db = _ready_db()
    preview = planner.portfolio_preview(
        db,
        month_key="2026-10",
        settings=_settings(),
        now=NOW,
    )
    counts = list(preview["daily_counts"].values())
    assert preview["ready"] is True
    assert len(counts) == 31
    assert sum(counts) == 150
    assert min(counts) == 4
    assert max(counts) == 5
    assert counts.count(5) == 26
    db.close()


def test_current_month_schedules_only_future_slots():
    db = _ready_db()
    now = datetime(2026, 9, 20, 20, 17, 31, tzinfo=UTC)
    preview = planner.portfolio_preview(
        db,
        month_key="2026-09",
        target_count=30,
        settings=_settings(pinterest_portfolio_reserve_percentage=0),
        now=now,
    )
    assert preview["ready"] is True
    planned = [slot for slot in preview["slots"] if slot["slot_kind"] == "PLANNED"]
    assert len(planned) == 30
    assert all(slot["scheduled_for"] > now for slot in planned)
    chicago = ZoneInfo("America/Chicago")
    assert min(slot["scheduled_for"].astimezone(chicago).date() for slot in planned) >= now.astimezone(chicago).date()
    db.close()


def test_past_month_blocks():
    db = _ready_db()
    preview = planner.portfolio_preview(
        db,
        month_key="2026-08",
        target_count=10,
        settings=_settings(),
        now=NOW,
    )
    assert preview["ready"] is False
    assert preview["blockers"] == ["PAST_MONTH"]
    db.close()


def test_exact_product_eligibility_filter_excludes_ineligible():
    db = _ready_db(products=5)
    _seed_ineligible_product(db)
    preview = planner.portfolio_preview(
        db,
        month_key="2026-10",
        target_count=5,
        settings=_settings(
            pinterest_portfolio_reserve_percentage=0,
            pinterest_product_monthly_cap=3,
        ),
        now=NOW,
    )
    assert preview["eligible_product_count"] == 5
    assert all(slot["product_id"] != "product-ineligible" for slot in preview["slots"])
    db.close()


def test_existing_publications_reduce_remaining_target_and_seed_product_cap():
    db = _ready_db(products=10)
    chicago = ZoneInfo("America/Chicago")
    _existing_publication(
        db,
        "product-000",
        datetime(2026, 10, 5, 12, 0, tzinfo=chicago).astimezone(UTC),
        1,
    )
    _existing_publication(
        db,
        "product-000",
        datetime(2026, 10, 7, 12, 0, tzinfo=chicago).astimezone(UTC),
        2,
    )
    preview = planner.portfolio_preview(
        db,
        month_key="2026-10",
        target_count=10,
        settings=_settings(
            pinterest_portfolio_reserve_percentage=0,
            pinterest_product_monthly_cap=3,
        ),
        now=NOW,
    )
    assert preview["existing_count"] == 2
    assert preview["remaining_count"] == 8
    assert preview["planned_count"] == 8
    product_counts = Counter(
        slot["product_id"]
        for slot in preview["slots"]
        if slot["slot_kind"] == "PLANNED"
    )
    assert product_counts["product-000"] <= 1
    db.close()


def test_product_monthly_cap_is_enforced():
    db = _ready_db(products=4)
    preview = planner.portfolio_preview(
        db,
        month_key="2026-10",
        target_count=8,
        settings=_settings(
            pinterest_product_monthly_cap=2,
            pinterest_portfolio_reserve_percentage=0,
        ),
        now=NOW,
    )
    assert preview["ready"] is True
    counts = Counter(slot["product_id"] for slot in preview["slots"])
    assert max(counts.values()) <= 2
    assert len(counts) == 4
    db.close()


def test_selection_diversifies_brands_boards_and_angles():
    db = _ready_db(products=12)
    preview = planner.portfolio_preview(
        db,
        month_key="2026-10",
        target_count=18,
        settings=_settings(
            pinterest_portfolio_reserve_percentage=0,
            pinterest_product_monthly_cap=3,
        ),
        now=NOW,
    )
    assert preview["ready"] is True
    slots = preview["slots"]
    assert len({slot["brand_key"] for slot in slots}) >= 6
    assert len({slot["board_key"] for slot in slots}) >= 2
    assert len({slot["angle_key"] for slot in slots}) >= 3
    db.close()


def test_missing_board_and_angle_mappings_are_reported_and_excluded():
    db = _db()
    _seed_taxonomy(db, include_guide_board=False, include_under50=False)
    _seed_products(db, 5)
    preview = planner.portfolio_preview(
        db,
        month_key="2026-10",
        target_count=5,
        settings=_settings(
            pinterest_portfolio_reserve_percentage=0,
            pinterest_product_monthly_cap=3,
        ),
        now=NOW,
    )
    assert preview["ready"] is True
    assert preview["missing_board_mappings"]["fragrance-guides"] == 5
    assert all(slot["board_key"] == "arabian-fragrance" for slot in preview["slots"])
    db.close()


def test_replacement_reserve_is_deterministic_and_disjoint():
    db = _ready_db(products=10)
    settings = _settings(
        pinterest_product_monthly_cap=3,
        pinterest_portfolio_reserve_percentage=0.20,
    )
    first = planner.portfolio_preview(
        db,
        month_key="2026-10",
        target_count=10,
        settings=settings,
        now=NOW,
    )
    second = planner.portfolio_preview(
        db,
        month_key="2026-10",
        target_count=10,
        settings=settings,
        now=NOW,
    )
    planned = [slot for slot in first["slots"] if slot["slot_kind"] == "PLANNED"]
    reserve = [slot for slot in first["slots"] if slot["slot_kind"] == "RESERVE"]
    assert len(planned) == 10
    assert len(reserve) == 2
    assert {s["candidate_fingerprint"] for s in planned}.isdisjoint(
        {s["candidate_fingerprint"] for s in reserve}
    )
    assert first["plan_fingerprint"] == second["plan_fingerprint"]
    assert [s["slot_fingerprint"] for s in first["slots"]] == [
        s["slot_fingerprint"] for s in second["slots"]
    ]
    db.close()


def test_insufficient_diversified_capacity_blocks_without_lowering_target():
    db = _ready_db(products=1)
    preview = planner.portfolio_preview(
        db,
        month_key="2026-10",
        target_count=2,
        settings=_settings(
            pinterest_product_monthly_cap=1,
            pinterest_portfolio_reserve_percentage=0,
        ),
        now=NOW,
    )
    assert preview["ready"] is False
    assert "INSUFFICIENT_DIVERSIFIED_CANDIDATES" in preview["blockers"]
    assert preview["remaining_count"] == 2
    assert preview["selected_capacity"] == 1
    db.close()


def test_schedule_conversion_is_dst_safe():
    db = _ready_db(products=20)
    preview = planner.portfolio_preview(
        db,
        month_key="2027-03",
        target_count=31,
        settings=_settings(
            pinterest_product_monthly_cap=3,
            pinterest_portfolio_reserve_percentage=0,
        ),
        now=NOW,
    )
    chicago = ZoneInfo("America/Chicago")
    offsets = {
        slot["scheduled_for"].astimezone(chicago).utcoffset()
        for slot in preview["slots"]
        if slot["slot_kind"] == "PLANNED"
    }
    assert timedelta(hours=-6) in offsets
    assert timedelta(hours=-5) in offsets
    db.close()


def test_mutation_is_disabled_by_default_and_preview_has_no_side_effects():
    db = _ready_db(products=10)
    before = (
        db.query(PinterestPortfolioPlan).count(),
        db.query(PinterestPortfolioSlot).count(),
        len(db.new),
        len(db.dirty),
        len(db.deleted),
    )
    preview = planner.portfolio_preview(
        db,
        month_key="2026-10",
        target_count=5,
        settings=_settings(pinterest_portfolio_reserve_percentage=0),
        now=NOW,
    )
    after = (
        db.query(PinterestPortfolioPlan).count(),
        db.query(PinterestPortfolioSlot).count(),
        len(db.new),
        len(db.dirty),
        len(db.deleted),
    )
    assert preview["ready"] is True
    assert preview["state_mutated"] is False
    assert preview["provider_called"] is False
    assert before == after

    with pytest.raises(planner.PortfolioPlanningError, match="PORTFOLIO_PLANNING_DISABLED"):
        planner.create_draft_portfolio_plan(
            db,
            month_key="2026-10",
            target_count=5,
            settings=_settings(),
            now=NOW,
        )
    db.close()


def test_create_plan_is_idempotent_and_same_month_drift_conflicts():
    db = _ready_db(products=10)
    enabled = _settings(
        pinterest_portfolio_planning_enabled=True,
        pinterest_portfolio_reserve_percentage=0,
    )
    first = planner.create_draft_portfolio_plan(
        db,
        month_key="2026-10",
        target_count=5,
        settings=enabled,
        now=NOW,
    )
    second = planner.create_draft_portfolio_plan(
        db,
        month_key="2026-10",
        target_count=5,
        settings=enabled,
        now=NOW,
    )
    assert first.id == second.id
    assert first.status == "DRAFT"
    assert db.query(PinterestPortfolioPlan).count() == 1
    assert db.query(PinterestPortfolioSlot).count() == 5

    with pytest.raises(planner.PortfolioPlanningError, match="PORTFOLIO_PLAN_MONTH_CONFLICT"):
        planner.create_draft_portfolio_plan(
            db,
            month_key="2026-10",
            target_count=6,
            settings=enabled,
            now=NOW,
        )
    db.close()
