from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

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
    PinterestPortfolioPlanItem,
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
        "pinterest_portfolio_planner_enabled": False,
        "pinterest_monthly_pin_target": 150,
        "pinterest_portfolio_max_pins_per_product": 3,
        "pinterest_portfolio_max_vendor_share": 0.25,
        "pinterest_portfolio_max_board_share": 0.40,
        "pinterest_portfolio_reserve_percentage": 0.10,
    }
    values.update(overrides)
    return Settings(**values)


def _seed_taxonomy(
    db,
    *,
    include_guide_board=True,
    include_under50=True,
):
    store = Store(
        id="store-1",
        name="Diamond Shelf",
        shop_domain="diamondshelf.us",
        market="US",
    )
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


def _seed_products(
    db,
    count=60,
    *,
    store_id="store-1",
    same_vendor=False,
):
    rows = []
    for index in range(count):
        vendor = "One Brand" if same_vendor else f"Brand {index % 10:02d}"
        product = Product(
            id=f"product-{index:03d}",
            store_id=store_id,
            shopify_product_id=f"shop-{index}",
            handle=f"product-{index}",
            title=f"Arabian Fragrance {index}",
            vendor=vendor,
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
            brand=vendor,
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


def _existing_publication(
    db,
    product_id: str,
    scheduled_for: datetime,
    sequence: int,
    *,
    status=PublicationStatus.SCHEDULED,
):
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
        status=status,
        scheduled_for=scheduled_for,
    )
    db.add_all([concept, draft, publication])
    db.commit()
    return publication


def _ready_db(products=60, **taxonomy_kwargs):
    db = _db()
    _seed_taxonomy(db, **taxonomy_kwargs)
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
    assert preview["planned_active_slots"] == 150
    assert preview["reserve_slots"] == 15
    assert len(preview["daily_pacing"]) == 30
    assert set(preview["daily_pacing"].values()) == {5}
    assert sum(preview["daily_pacing"].values()) == 150
    assert all(item["planned_date"] is not None for item in preview["items"] if not item["is_reserve"])
    assert all(item["planned_date"] is None for item in preview["items"] if item["is_reserve"])
    db.close()


def test_150_pin_31_day_month_evenly_distributes_four_and_five():
    db = _ready_db()
    preview = planner.portfolio_preview(
        db,
        month_key="2026-10",
        settings=_settings(),
        now=NOW,
    )
    counts = list(preview["daily_pacing"].values())
    assert preview["ready"] is True
    assert len(counts) == 31
    assert sum(counts) == 150
    assert min(counts) == 4
    assert max(counts) == 5
    assert counts.count(5) == 26
    db.close()


def test_past_month_blocks_and_current_month_uses_nonpast_dates():
    db = _ready_db()
    past = planner.portfolio_preview(
        db,
        month_key="2026-08",
        target_pins=10,
        settings=_settings(pinterest_portfolio_reserve_percentage=0),
        now=NOW,
    )
    assert past["ready"] is False
    assert past["blockers"] == ["PAST_MONTH"]

    current = planner.portfolio_preview(
        db,
        month_key="2026-09",
        target_pins=20,
        settings=_settings(pinterest_portfolio_reserve_percentage=0),
        now=NOW,
    )
    assert current["ready"] is True
    active = [item for item in current["items"] if not item["is_reserve"]]
    assert len(active) == 20
    assert min(item["planned_date"] for item in active).isoformat() >= "2026-09-20"
    db.close()


def test_exact_product_eligibility_filter_excludes_ineligible():
    db = _ready_db(products=5)
    _seed_ineligible_product(db)
    preview = planner.portfolio_preview(
        db,
        month_key="2026-10",
        target_pins=5,
        settings=_settings(
            pinterest_portfolio_reserve_percentage=0,
            pinterest_portfolio_max_pins_per_product=3,
        ),
        now=NOW,
    )
    assert preview["eligible_product_count"] == 5
    assert all(item["product_id"] != "product-ineligible" for item in preview["items"])
    db.close()


def test_publish_unknown_blocks_planning_globally():
    db = _ready_db(products=5)
    _existing_publication(
        db,
        "product-000",
        datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
        1,
        status=PublicationStatus.PUBLISH_UNKNOWN,
    )
    preview = planner.portfolio_preview(
        db,
        month_key="2026-10",
        target_pins=5,
        settings=_settings(pinterest_portfolio_reserve_percentage=0),
        now=NOW,
    )
    assert preview["ready"] is False
    assert preview["blockers"] == ["PUBLISH_UNKNOWN_PRESENT"]
    assert preview["publish_unknown_count"] == 1
    db.close()


def test_existing_commitments_reduce_target_and_seed_product_cap():
    db = _ready_db(products=10)
    _existing_publication(
        db,
        "product-000",
        datetime(2026, 10, 5, 12, 0, tzinfo=UTC),
        1,
    )
    _existing_publication(
        db,
        "product-000",
        datetime(2026, 10, 7, 12, 0, tzinfo=UTC),
        2,
    )
    preview = planner.portfolio_preview(
        db,
        month_key="2026-10",
        target_pins=10,
        settings=_settings(
            pinterest_portfolio_reserve_percentage=0,
            pinterest_portfolio_max_pins_per_product=3,
        ),
        now=NOW,
    )
    assert preview["existing_commitments"] == 2
    assert preview["remaining_active_slots"] == 8
    assert preview["planned_active_slots"] == 8
    product_counts = Counter(
        item["product_id"] for item in preview["items"] if not item["is_reserve"]
    )
    assert product_counts["product-000"] <= 1
    db.close()


def test_unique_product_first_allocation():
    db = _ready_db(products=10)
    preview = planner.portfolio_preview(
        db,
        month_key="2026-10",
        target_pins=10,
        settings=_settings(
            pinterest_portfolio_reserve_percentage=0,
            pinterest_portfolio_max_pins_per_product=3,
            pinterest_portfolio_max_vendor_share=1.0,
            pinterest_portfolio_max_board_share=1.0,
        ),
        now=NOW,
    )
    active = [item for item in preview["items"] if not item["is_reserve"]]
    assert preview["ready"] is True
    assert len({item["product_id"] for item in active}) == 10
    db.close()


def test_hard_product_monthly_cap_is_never_relaxed():
    db = _ready_db(products=4)
    preview = planner.portfolio_preview(
        db,
        month_key="2026-10",
        target_pins=8,
        settings=_settings(
            pinterest_portfolio_reserve_percentage=0,
            pinterest_portfolio_max_pins_per_product=2,
            pinterest_portfolio_max_vendor_share=1.0,
            pinterest_portfolio_max_board_share=1.0,
        ),
        now=NOW,
    )
    assert preview["ready"] is True
    counts = Counter(item["product_id"] for item in preview["items"])
    assert max(counts.values()) <= 2
    assert len(counts) == 4

    blocked = planner.portfolio_preview(
        db,
        month_key="2026-11",
        target_pins=9,
        settings=_settings(
            pinterest_portfolio_reserve_percentage=0,
            pinterest_portfolio_max_pins_per_product=2,
            pinterest_portfolio_max_vendor_share=1.0,
            pinterest_portfolio_max_board_share=1.0,
        ),
        now=NOW,
    )
    assert blocked["ready"] is False
    assert "INSUFFICIENT_ACTIVE_CAPACITY" in blocked["blockers"]
    db.close()


def test_vendor_cap_relaxes_deterministically_when_required():
    db = _db()
    _seed_taxonomy(db)
    _seed_products(db, 6, same_vendor=True)
    preview = planner.portfolio_preview(
        db,
        month_key="2026-10",
        target_pins=6,
        settings=_settings(
            pinterest_portfolio_reserve_percentage=0,
            pinterest_portfolio_max_pins_per_product=1,
            pinterest_portfolio_max_vendor_share=0.34,
            pinterest_portfolio_max_board_share=1.0,
        ),
        now=NOW,
    )
    assert preview["ready"] is True
    assert preview["cap_policy"]["vendor_limit"] == 3
    assert preview["cap_relaxation"]["used"] is True
    assert preview["cap_relaxation"]["vendor_cap_relaxed"] is True
    assert any(
        item["selection_metadata"]["selection_stage"] == "RELAXED"
        for item in preview["items"]
    )
    db.close()


def test_board_cap_relaxes_deterministically_when_required():
    db = _db()
    _seed_taxonomy(db, include_guide_board=False)
    _seed_products(db, 6)
    preview = planner.portfolio_preview(
        db,
        month_key="2026-10",
        target_pins=6,
        settings=_settings(
            pinterest_portfolio_reserve_percentage=0,
            pinterest_portfolio_max_pins_per_product=1,
            pinterest_portfolio_max_vendor_share=1.0,
            pinterest_portfolio_max_board_share=0.34,
        ),
        now=NOW,
    )
    assert preview["ready"] is True
    assert preview["cap_policy"]["board_limit"] == 3
    assert preview["cap_relaxation"]["used"] is True
    assert preview["cap_relaxation"]["board_cap_relaxed"] is True
    db.close()


def test_missing_taxonomy_mappings_are_product_level_and_excluded():
    db = _db()
    _seed_taxonomy(db, include_guide_board=False, include_under50=False)
    _seed_products(db, 5)
    preview = planner.portfolio_preview(
        db,
        month_key="2026-10",
        target_pins=5,
        settings=_settings(
            pinterest_portfolio_reserve_percentage=0,
            pinterest_portfolio_max_pins_per_product=3,
        ),
        now=NOW,
    )
    assert preview["ready"] is True
    assert preview["taxonomy_gaps"]["boards"]["fragrance-guides"] == 5
    assert all(
        item["board_key_snapshot"] == "arabian-fragrance"
        for item in preview["items"]
    )
    db.close()


def test_missing_angle_mapping_is_reported_once_per_product():
    db = _db()
    _seed_taxonomy(db, include_guide_board=True, include_under50=False)
    _seed_products(db, 5)
    preview = planner.portfolio_preview(
        db,
        month_key="2026-10",
        target_pins=5,
        settings=_settings(
            pinterest_portfolio_reserve_percentage=0,
            pinterest_portfolio_max_pins_per_product=3,
        ),
        now=NOW,
    )
    assert preview["taxonomy_gaps"]["angles"]["under-50"] == 5
    db.close()


def test_replacement_reserve_is_deterministic_disjoint_and_undated():
    db = _ready_db(products=10)
    settings = _settings(
        pinterest_portfolio_max_pins_per_product=3,
        pinterest_portfolio_max_vendor_share=1.0,
        pinterest_portfolio_max_board_share=1.0,
        pinterest_portfolio_reserve_percentage=0.20,
    )
    first = planner.portfolio_preview(
        db,
        month_key="2026-10",
        target_pins=10,
        settings=settings,
        now=NOW,
    )
    second = planner.portfolio_preview(
        db,
        month_key="2026-10",
        target_pins=10,
        settings=settings,
        now=NOW,
    )
    planned = [item for item in first["items"] if not item["is_reserve"]]
    reserve = [item for item in first["items"] if item["is_reserve"]]
    assert len(planned) == 10
    assert len(reserve) == 2
    planned_candidates = {
        item["selection_metadata"]["candidate_fingerprint"] for item in planned
    }
    reserve_candidates = {
        item["selection_metadata"]["candidate_fingerprint"] for item in reserve
    }
    assert planned_candidates.isdisjoint(reserve_candidates)
    assert all(item["planned_date"] is None for item in reserve)
    assert first["preview_fingerprint"] == second["preview_fingerprint"]
    assert [item["item_fingerprint"] for item in first["items"]] == [
        item["item_fingerprint"] for item in second["items"]
    ]
    db.close()


def test_preview_fingerprint_changes_with_target_or_catalog_inputs():
    db = _ready_db(products=10)
    settings = _settings(pinterest_portfolio_reserve_percentage=0)
    first = planner.portfolio_preview(
        db,
        month_key="2026-10",
        target_pins=6,
        settings=settings,
        now=NOW,
    )
    second = planner.portfolio_preview(
        db,
        month_key="2026-10",
        target_pins=7,
        settings=settings,
        now=NOW,
    )
    assert first["input_fingerprint"] != second["input_fingerprint"]
    assert first["preview_fingerprint"] != second["preview_fingerprint"]
    db.close()


def test_preview_has_zero_db_provider_ai_or_downstream_side_effects():
    db = _ready_db(products=5)
    before = (
        db.query(PinterestPortfolioPlan).count(),
        db.query(PinterestPortfolioPlanItem).count(),
        db.query(PinConcept).count(),
        db.query(PinDraft).count(),
        db.query(PinPublication).count(),
        len(db.new),
        len(db.dirty),
        len(db.deleted),
    )
    result = planner.portfolio_preview(
        db,
        month_key="2026-10",
        target_pins=5,
        settings=_settings(pinterest_portfolio_reserve_percentage=0),
        now=NOW,
    )
    after = (
        db.query(PinterestPortfolioPlan).count(),
        db.query(PinterestPortfolioPlanItem).count(),
        db.query(PinConcept).count(),
        db.query(PinDraft).count(),
        db.query(PinPublication).count(),
        len(db.new),
        len(db.dirty),
        len(db.deleted),
    )
    assert result["ready"] is True
    assert result["state_mutated"] is False
    assert result["provider_called"] is False
    assert result["ai_called"] is False
    assert before == after
    db.close()


def test_plan_creation_is_disabled_by_default():
    db = _ready_db(products=5)
    with pytest.raises(planner.PortfolioPlanningError, match="PORTFOLIO_PLANNER_DISABLED"):
        planner.create_draft_portfolio_plan(
            db,
            month_key="2026-10",
            target_pins=5,
            settings=_settings(),
            now=NOW,
        )
    assert db.query(PinterestPortfolioPlan).count() == 0
    assert db.query(PinterestPortfolioPlanItem).count() == 0
    db.close()


def test_create_plan_is_idempotent_for_exact_fingerprint():
    db = _ready_db(products=5)
    settings = _settings(
        pinterest_portfolio_planner_enabled=True,
        pinterest_portfolio_reserve_percentage=0,
        pinterest_portfolio_max_vendor_share=1.0,
        pinterest_portfolio_max_board_share=1.0,
    )
    first = planner.create_draft_portfolio_plan(
        db,
        month_key="2026-10",
        target_pins=5,
        settings=settings,
        now=NOW,
    )
    second = planner.create_draft_portfolio_plan(
        db,
        month_key="2026-10",
        target_pins=5,
        settings=settings,
        now=NOW,
    )
    assert first.id == second.id
    assert first.status == "DRAFT"
    assert first.target_pins == 5
    assert first.planned_active_slots == 5
    assert first.reserve_slots == 0
    assert db.query(PinterestPortfolioPlan).count() == 1
    items = list(db.scalars(
        select(PinterestPortfolioPlanItem)
        .where(PinterestPortfolioPlanItem.plan_id == first.id)
        .order_by(PinterestPortfolioPlanItem.slot_index)
    ).all())
    assert len(items) == 5
    assert len({item.item_fingerprint for item in items}) == 5
    assert all(item.status == "PLANNED" for item in items)
    assert all(item.publication_id is None for item in items)
    db.close()


def test_conflicting_same_month_draft_plan_blocks():
    db = _ready_db(products=6)
    settings = _settings(
        pinterest_portfolio_planner_enabled=True,
        pinterest_portfolio_reserve_percentage=0,
        pinterest_portfolio_max_vendor_share=1.0,
        pinterest_portfolio_max_board_share=1.0,
    )
    planner.create_draft_portfolio_plan(
        db,
        month_key="2026-10",
        target_pins=5,
        settings=settings,
        now=NOW,
    )
    with pytest.raises(
        planner.PortfolioPlanningError,
        match="PORTFOLIO_PLAN_MONTH_CONFLICT",
    ):
        planner.create_draft_portfolio_plan(
            db,
            month_key="2026-10",
            target_pins=6,
            settings=settings,
            now=NOW,
        )
    assert db.query(PinterestPortfolioPlan).count() == 1
    db.close()


def test_settings_defaults_are_fail_closed_and_bounded():
    settings = _settings()
    assert settings.pinterest_portfolio_planner_enabled is False
    assert settings.pinterest_monthly_pin_target == 150
    assert settings.pinterest_portfolio_max_pins_per_product == 3
    assert 0 < settings.pinterest_portfolio_max_vendor_share <= 1
    assert 0 < settings.pinterest_portfolio_max_board_share <= 1
    assert 0 <= settings.pinterest_portfolio_reserve_percentage <= 1
