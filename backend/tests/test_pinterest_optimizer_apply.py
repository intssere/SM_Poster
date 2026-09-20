from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.db.base import Base
from app.models.domain import (
    Board,
    ContentAngle,
    PinterestOptimizerApplication,
    PinterestPortfolioPlan,
    PinterestPortfolioPlanItem,
    Product,
    Store,
)
from app.services import pinterest_optimizer_apply as applysvc


AS_OF = datetime(2026, 9, 20, 16, 0, tzinfo=timezone.utc)


def _db():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, Session()


def _settings(**overrides):
    values = {
        "database_url": "sqlite+pysqlite:///:memory:",
        "pinterest_portfolio_activation_enabled": False,
        "pinterest_optimizer_apply_enabled": False,
        "pinterest_optimizer_enabled": False,
        "pinterest_optimizer_exploit_share": 0.70,
        "pinterest_portfolio_max_pins_per_product": 5,
        "pinterest_portfolio_max_board_share": 1.0,
    }
    values.update(overrides)
    return Settings(**values)


def _enabled_settings(**overrides):
    return _settings(
        pinterest_portfolio_activation_enabled=True,
        pinterest_optimizer_apply_enabled=True,
        **overrides,
    )


def _seed_plan(db, *, status="DRAFT", frozen=False):
    db.add(Store(id="store-1", name="Diamond Shelf", shop_domain="diamondshelf.us"))
    for index in range(1, 5):
        db.add(Product(
            id=f"product-{index}",
            store_id="store-1",
            shopify_product_id=f"shop-{index}",
            handle=f"p-{index}",
            title=f"Product {index}",
            product_url=f"https://diamondshelf.us/products/p-{index}",
        ))
        db.add(Board(
            id=f"board-{index}",
            store_id="store-1",
            name=f"Board {index}",
            slug=f"board-{index}",
            rules={},
            active=True,
        ))
        db.add(ContentAngle(
            id=f"angle-{index}",
            key=f"angle-{index}",
            name=f"Angle {index}",
            rules={},
            active=True,
        ))
    db.flush()

    plan = PinterestPortfolioPlan(
        id="plan-1",
        store_id="store-1",
        month_start=date(2026, 9, 1),
        month_end=date(2026, 9, 30),
        target_pins=3,
        existing_commitments=0,
        planned_active_slots=2,
        reserve_slots=1,
        policy_version="PINTEREST_PORTFOLIO_V2",
        input_fingerprint="a" * 64,
        plan_fingerprint="b" * 64,
        status=status,
        metadata_json={"existing": "keep"},
    )
    db.add(plan)
    db.flush()

    rows = [
        PinterestPortfolioPlanItem(
            id="item-a",
            plan_id=plan.id,
            slot_index=1,
            is_reserve=False,
            planned_date=date(2026, 9, 21),
            product_id="product-1",
            local_board_id="board-1",
            board_key_snapshot="board-1",
            content_angle_id="angle-1",
            angle_key_snapshot="angle-1",
            seed_keywords=["alpha"],
            selection_score=Decimal("10.000000"),
            selection_metadata={"source": "planner"},
            item_fingerprint="1" * 64,
            status="PLANNED",
        ),
        PinterestPortfolioPlanItem(
            id="item-b",
            plan_id=plan.id,
            slot_index=2,
            is_reserve=False,
            planned_date=date(2026, 9, 22),
            product_id="product-2",
            local_board_id="board-2",
            board_key_snapshot="board-2",
            content_angle_id="angle-2",
            angle_key_snapshot="angle-2",
            seed_keywords=["beta"],
            selection_score=Decimal("9.000000"),
            selection_metadata={"source": "planner"},
            item_fingerprint="2" * 64,
            status="PLANNED",
        ),
        PinterestPortfolioPlanItem(
            id="item-r",
            plan_id=plan.id,
            slot_index=3,
            is_reserve=True,
            planned_date=None,
            product_id="product-3",
            local_board_id="board-3",
            board_key_snapshot="board-3",
            content_angle_id="angle-3",
            angle_key_snapshot="angle-3",
            seed_keywords=["reserve"],
            selection_score=Decimal("8.000000"),
            selection_metadata={"source": "planner", "retain": {"x": 1}},
            item_fingerprint="3" * 64,
            status="PLANNED",
        ),
    ]
    if frozen:
        rows.append(PinterestPortfolioPlanItem(
            id="item-frozen",
            plan_id=plan.id,
            slot_index=4,
            is_reserve=False,
            planned_date=date(2026, 9, 23),
            product_id="product-4",
            local_board_id="board-4",
            board_key_snapshot="board-4",
            content_angle_id="angle-4",
            angle_key_snapshot="angle-4",
            seed_keywords=["frozen"],
            selection_score=Decimal("7.000000"),
            selection_metadata={"frozen": True},
            item_fingerprint="4" * 64,
            status="GENERATED",
        ))
    db.add_all(rows)
    db.commit()
    return plan


def _preview(*, frozen_count=0, fingerprint="optimizer-fp-1"):
    return {
        "policy_version": "PINTEREST_OPTIMIZER_V1",
        "ready": True,
        "blockers": [],
        "optimizer_ready": True,
        "learning_fingerprint": "learning-fp-1",
        "frozen_item_count": frozen_count,
        "optimizable_item_count": 3,
        "exploit_count": 2,
        "explore_count": 1,
        "optimizer_fingerprint": fingerprint,
        "recommendations": [
            {
                "recommended_position": 1,
                "target_slot_index": 1,
                "recommended_planned_date": "2026-09-21",
                "item_id": "item-r",
                "selection_reason": "EXPLOIT",
                "evidence_score": "0.900000000000",
            },
            {
                "recommended_position": 2,
                "target_slot_index": 2,
                "recommended_planned_date": "2026-09-22",
                "item_id": "item-b",
                "selection_reason": "EXPLORE",
                "evidence_score": "0.500000000000",
            },
            {
                "recommended_position": 3,
                "target_slot_index": 3,
                "recommended_planned_date": None,
                "item_id": "item-a",
                "selection_reason": "EXPLOIT",
                "evidence_score": "0.800000000000",
            },
        ],
    }


def _patch_preview(monkeypatch, *, frozen_count=0, fingerprint="optimizer-fp-1"):
    monkeypatch.setattr(
        applysvc,
        "optimizer_preview",
        lambda *args, **kwargs: _preview(
            frozen_count=frozen_count,
            fingerprint=fingerprint,
        ),
    )


def test_apply_disabled_by_default_refuses(monkeypatch):
    engine, db = _db()
    plan = _seed_plan(db)
    _patch_preview(monkeypatch)
    ready = applysvc.optimizer_apply_readiness(db, plan.id, settings=_settings(), as_of_at=AS_OF)

    assert ready["structurally_ready"] is True
    assert ready["ready"] is False
    assert ready["blockers"][-2:] == [
        "PORTFOLIO_ACTIVATION_DISABLED",
        "OPTIMIZER_APPLY_DISABLED",
    ]
    with pytest.raises(applysvc.OptimizerApplyError, match="PORTFOLIO_ACTIVATION_DISABLED"):
        applysvc.apply_optimizer(
            db,
            plan.id,
            expected_optimizer_fingerprint=ready["optimizer_fingerprint"],
            expected_input_state_fingerprint=ready["input_state_fingerprint"],
            settings=_settings(),
            as_of_at=AS_OF,
        )
    assert db.get(PinterestPortfolioPlan, plan.id).status == "DRAFT"
    assert db.query(PinterestOptimizerApplication).count() == 0
    db.close(); engine.dispose()


def test_draft_to_active_apply_swaps_active_and_reserve_roles(monkeypatch):
    engine, db = _db()
    plan = _seed_plan(db)
    _patch_preview(monkeypatch)
    settings = _enabled_settings()
    ready = applysvc.optimizer_apply_readiness(db, plan.id, settings=settings, as_of_at=AS_OF)

    application = applysvc.apply_optimizer(
        db,
        plan.id,
        expected_optimizer_fingerprint=ready["optimizer_fingerprint"],
        expected_input_state_fingerprint=ready["input_state_fingerprint"],
        settings=settings,
        as_of_at=AS_OF,
        now=AS_OF,
    )

    assert db.get(PinterestPortfolioPlan, plan.id).status == "ACTIVE"
    a = db.get(PinterestPortfolioPlanItem, "item-a")
    b = db.get(PinterestPortfolioPlanItem, "item-b")
    reserve = db.get(PinterestPortfolioPlanItem, "item-r")

    assert (a.slot_index, a.is_reserve, a.planned_date) == (1, True, None)
    assert (b.slot_index, b.is_reserve, b.planned_date) == (2, False, date(2026, 9, 22))
    assert (reserve.slot_index, reserve.is_reserve, reserve.planned_date) == (
        3, False, date(2026, 9, 21)
    )
    assert reserve.selection_metadata["source"] == "planner"
    assert reserve.selection_metadata["retain"] == {"x": 1}
    assert reserve.selection_metadata[applysvc.OPTIMIZER_METADATA_KEY]["target_slot_index"] == 1
    assert application.status == "APPLIED"
    assert application.optimizer_fingerprint == "optimizer-fp-1"
    assert application.optimizable_item_count == 3
    assert application.exploit_count == 2
    assert application.explore_count == 1
    db.close(); engine.dispose()


def test_immutable_item_identity_fields_never_change(monkeypatch):
    engine, db = _db()
    plan = _seed_plan(db)
    _patch_preview(monkeypatch)
    settings = _enabled_settings()
    before = {
        item.id: (
            item.slot_index,
            item.product_id,
            item.local_board_id,
            item.board_key_snapshot,
            item.content_angle_id,
            item.angle_key_snapshot,
            list(item.seed_keywords),
            str(item.selection_score),
            item.item_fingerprint,
        )
        for item in db.query(PinterestPortfolioPlanItem).all()
    }
    ready = applysvc.optimizer_apply_readiness(db, plan.id, settings=settings, as_of_at=AS_OF)
    applysvc.apply_optimizer(
        db, plan.id,
        expected_optimizer_fingerprint=ready["optimizer_fingerprint"],
        expected_input_state_fingerprint=ready["input_state_fingerprint"],
        settings=settings, as_of_at=AS_OF,
    )
    after = {
        item.id: (
            item.slot_index,
            item.product_id,
            item.local_board_id,
            item.board_key_snapshot,
            item.content_angle_id,
            item.angle_key_snapshot,
            list(item.seed_keywords),
            str(item.selection_score),
            item.item_fingerprint,
        )
        for item in db.query(PinterestPortfolioPlanItem).all()
    }
    assert before == after
    db.close(); engine.dispose()


def test_frozen_rows_are_field_for_field_unchanged(monkeypatch):
    engine, db = _db()
    plan = _seed_plan(db, frozen=True)
    _patch_preview(monkeypatch, frozen_count=1)
    settings = _enabled_settings()
    frozen = db.get(PinterestPortfolioPlanItem, "item-frozen")
    before = applysvc._item_state(frozen)

    ready = applysvc.optimizer_apply_readiness(db, plan.id, settings=settings, as_of_at=AS_OF)
    applysvc.apply_optimizer(
        db, plan.id,
        expected_optimizer_fingerprint=ready["optimizer_fingerprint"],
        expected_input_state_fingerprint=ready["input_state_fingerprint"],
        settings=settings, as_of_at=AS_OF,
    )

    assert applysvc._item_state(db.get(PinterestPortfolioPlanItem, "item-frozen")) == before
    db.close(); engine.dispose()


def test_exact_fingerprint_and_state_binding_are_required(monkeypatch):
    engine, db = _db()
    plan = _seed_plan(db)
    _patch_preview(monkeypatch)
    settings = _enabled_settings()
    ready = applysvc.optimizer_apply_readiness(db, plan.id, settings=settings, as_of_at=AS_OF)

    with pytest.raises(applysvc.OptimizerApplyError, match="OPTIMIZER_FINGERPRINT_MISMATCH"):
        applysvc.apply_optimizer(
            db, plan.id,
            expected_optimizer_fingerprint="wrong",
            expected_input_state_fingerprint=ready["input_state_fingerprint"],
            settings=settings, as_of_at=AS_OF,
        )

    item = db.get(PinterestPortfolioPlanItem, "item-a")
    item.selection_metadata = {"source": "changed"}
    db.commit()
    with pytest.raises(applysvc.OptimizerApplyError, match="OPTIMIZER_INPUT_STATE_DRIFT"):
        applysvc.apply_optimizer(
            db, plan.id,
            expected_optimizer_fingerprint=ready["optimizer_fingerprint"],
            expected_input_state_fingerprint=ready["input_state_fingerprint"],
            settings=settings, as_of_at=AS_OF,
        )
    assert db.query(PinterestOptimizerApplication).count() == 0
    db.close(); engine.dispose()


def test_exact_repeat_is_idempotent_and_different_second_application_blocks(monkeypatch):
    engine, db = _db()
    plan = _seed_plan(db)
    _patch_preview(monkeypatch)
    settings = _enabled_settings()
    ready = applysvc.optimizer_apply_readiness(db, plan.id, settings=settings, as_of_at=AS_OF)

    first = applysvc.apply_optimizer(
        db, plan.id,
        expected_optimizer_fingerprint=ready["optimizer_fingerprint"],
        expected_input_state_fingerprint=ready["input_state_fingerprint"],
        settings=settings, as_of_at=AS_OF,
    )
    second = applysvc.apply_optimizer(
        db, plan.id,
        expected_optimizer_fingerprint=ready["optimizer_fingerprint"],
        expected_input_state_fingerprint=ready["input_state_fingerprint"],
        settings=settings, as_of_at=AS_OF,
    )
    assert first.id == second.id
    assert db.query(PinterestOptimizerApplication).count() == 1

    with pytest.raises(applysvc.OptimizerApplyError, match="OPTIMIZER_APPLICATION_CONFLICT"):
        applysvc.apply_optimizer(
            db, plan.id,
            expected_optimizer_fingerprint="different",
            expected_input_state_fingerprint=ready["input_state_fingerprint"],
            settings=settings, as_of_at=AS_OF,
        )
    db.close(); engine.dispose()


@pytest.mark.parametrize("status", ["ACTIVE", "COMPLETED", "CANCELLED"])
def test_first_application_requires_draft_plan(monkeypatch, status):
    engine, db = _db()
    plan = _seed_plan(db, status=status)
    _patch_preview(monkeypatch)
    result = applysvc.optimizer_apply_readiness(
        db, plan.id, settings=_enabled_settings(), as_of_at=AS_OF
    )
    assert result["structurally_ready"] is False
    assert "PLAN_NOT_DRAFT" in result["blockers"]
    db.close(); engine.dispose()


def test_publish_unknown_blocks_without_mutation(monkeypatch):
    engine, db = _db()
    plan = _seed_plan(db)
    _patch_preview(monkeypatch)
    monkeypatch.setattr(applysvc, "_publish_unknown_count", lambda db_arg: 1)

    result = applysvc.optimizer_apply_readiness(
        db, plan.id, settings=_enabled_settings(), as_of_at=AS_OF
    )
    assert result["structurally_ready"] is False
    assert "PUBLISH_UNKNOWN_PRESENT" in result["blockers"]
    assert db.get(PinterestPortfolioPlan, plan.id).status == "DRAFT"
    db.close(); engine.dispose()


def test_readiness_is_read_only_and_provider_ai_free(monkeypatch):
    engine, db = _db()
    plan = _seed_plan(db)
    _patch_preview(monkeypatch)
    before = [
        applysvc._item_state(item)
        for item in db.scalars(
            select(PinterestPortfolioPlanItem)
            .where(PinterestPortfolioPlanItem.plan_id == plan.id)
            .order_by(PinterestPortfolioPlanItem.id)
        ).all()
    ]

    result = applysvc.optimizer_apply_readiness(
        db, plan.id, settings=_settings(), as_of_at=AS_OF
    )
    after = [
        applysvc._item_state(item)
        for item in db.scalars(
            select(PinterestPortfolioPlanItem)
            .where(PinterestPortfolioPlanItem.plan_id == plan.id)
            .order_by(PinterestPortfolioPlanItem.id)
        ).all()
    ]
    assert before == after
    assert result["state_mutated"] is False
    assert result["provider_called"] is False
    assert result["ai_called"] is False
    assert db.query(PinterestOptimizerApplication).count() == 0
    assert len(db.new) == len(db.dirty) == len(db.deleted) == 0
    db.close(); engine.dispose()
