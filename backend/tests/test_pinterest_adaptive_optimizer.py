from datetime import date, datetime, timezone
from decimal import Decimal
import math

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.db.base import Base
from app.models.domain import (
    Board,
    ContentAngle,
    PinterestPortfolioPlan,
    PinterestPortfolioPlanItem,
    Product,
    Store,
)
from app.services import pinterest_adaptive_optimizer as optimizer


AS_OF = datetime(2026, 9, 20, 14, 0, tzinfo=timezone.utc)


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
        "pinterest_optimizer_enabled": False,
        "pinterest_optimizer_exploit_share": 0.70,
        "pinterest_portfolio_max_pins_per_product": 3,
        "pinterest_portfolio_max_board_share": 0.75,
        "pinterest_learning_prior_impressions": 500,
        "pinterest_learning_min_total_publications": 2,
        "pinterest_learning_min_dimension_samples": 2,
    }
    values.update(overrides)
    return Settings(**values)


def _cap_metadata(
    *,
    target_pins: int,
    max_pins_per_product: int = 3,
    max_vendor_share: float = 1.0,
    max_board_share: float = 1.0,
    vendor_cap_relaxed: bool = False,
    board_cap_relaxed: bool = False,
):
    vendor_limit = max(1, math.ceil(target_pins * max_vendor_share))
    board_limit = max(1, math.ceil(target_pins * max_board_share))
    return {
        "cap_policy": {
            "max_pins_per_product": max_pins_per_product,
            "max_vendor_share": max_vendor_share,
            "max_board_share": max_board_share,
            "vendor_limit": vendor_limit,
            "board_limit": board_limit,
        },
        "cap_relaxation": {
            "used": bool(vendor_cap_relaxed or board_cap_relaxed),
            "vendor_cap_relaxed": vendor_cap_relaxed,
            "board_cap_relaxed": board_cap_relaxed,
            "vendor_limit": vendor_limit,
            "board_limit": board_limit,
        },
    }


def _seed_plan(
    db,
    *,
    store_id="store-1",
    plan_id="plan-1",
    item_specs=None,
    target_pins=150,
    cap_metadata=None,
):
    db.add(Store(id=store_id, name=store_id, shop_domain=f"{store_id}.example"))
    db.flush()

    plan = PinterestPortfolioPlan(
        id=plan_id,
        store_id=store_id,
        month_start=date(2026, 9, 1),
        month_end=date(2026, 9, 30),
        target_pins=target_pins,
        existing_commitments=0,
        planned_active_slots=0,
        reserve_slots=0,
        policy_version="PINTEREST_PORTFOLIO_V2",
        input_fingerprint=(plan_id.replace("-", "") + "a" * 64)[:64],
        plan_fingerprint=(plan_id.replace("-", "") + "b" * 64)[:64],
        status="DRAFT",
        metadata_json=cap_metadata or _cap_metadata(target_pins=target_pins),
    )
    db.add(plan)
    db.flush()

    specs = item_specs or []
    for i, spec in enumerate(specs, start=1):
        product_id = spec.get("product_id", f"product-{i}")
        board_id = spec.get("board_id", f"board-{i}")
        angle_id = spec.get("angle_id", f"angle-{i}")
        if db.get(Product, product_id) is None:
            db.add(Product(
                id=product_id,
                store_id=store_id,
                shopify_product_id=f"shop-{product_id}",
                handle=product_id,
                title=product_id,
                product_url=f"https://example.com/{product_id}",
            ))
        if db.get(Board, board_id) is None:
            db.add(Board(
                id=board_id,
                store_id=store_id,
                name=board_id,
                slug=board_id,
                rules={},
                active=True,
            ))
        if db.get(ContentAngle, angle_id) is None:
            db.add(ContentAngle(
                id=angle_id,
                key=angle_id,
                name=angle_id,
                rules={},
                active=True,
            ))
        db.flush()

        is_reserve = bool(spec.get("is_reserve", False))
        selection_metadata = {
            "candidate_fingerprint": spec.get(
                "candidate_fingerprint",
                (f"candidate-{plan_id}-{i}" + "d" * 64)[:64],
            ),
            "vendor_key": spec.get("vendor_key", "vendor-default"),
            "selection_stage": spec.get(
                "selection_stage",
                "RESERVE" if is_reserve else "STRICT",
            ),
            "relaxed_vendor_cap": bool(spec.get("relaxed_vendor_cap", False)),
            "relaxed_board_cap": bool(spec.get("relaxed_board_cap", False)),
            **spec.get("selection_metadata", {}),
        }
        db.add(PinterestPortfolioPlanItem(
            id=spec.get("id", f"item-{i}"),
            plan_id=plan.id,
            slot_index=spec.get("slot_index", i),
            is_reserve=is_reserve,
            planned_date=spec.get(
                "planned_date",
                None if is_reserve else date(2026, 9, i),
            ),
            product_id=product_id,
            local_board_id=board_id,
            board_key_snapshot=board_id,
            content_angle_id=angle_id,
            angle_key_snapshot=angle_id,
            seed_keywords=[],
            selection_score=Decimal(str(spec.get("selection_score", "1.0"))),
            selection_metadata=selection_metadata,
            item_fingerprint=(f"{plan_id}-{i}" + "c" * 64)[:64],
            status=spec.get("status", "PLANNED"),
            publication_id=spec.get("publication_id"),
        ))
    db.commit()
    return plan


def _learning(*, ready=True, rankings=None, fingerprint="learn-1"):
    return {
        "optimizer_ready": ready,
        "learning_fingerprint": fingerprint,
        "publication_count": 20 if ready else 1,
        "rankings": rankings or {
            "product": [],
            "board": [],
            "content_angle": [],
        },
    }


def _rank(entity_key, rank, score="0.100000000000", publication_count=5, impressions=1000):
    return {
        "entity_key": entity_key,
        "rank": rank,
        "score": score,
        "publication_count": publication_count,
        "impressions": impressions,
    }


def test_only_uncommitted_planned_items_are_optimized(monkeypatch):
    engine, db = _db()
    plan = _seed_plan(db, item_specs=[
        {"id": "planned", "status": "PLANNED"},
        {"id": "generated", "status": "GENERATED"},
        {"id": "scheduled", "status": "SCHEDULED", "publication_id": "pub-x"},
        {"id": "planned-bound", "status": "PLANNED", "publication_id": "pub-y"},
    ])
    monkeypatch.setattr(optimizer, "learning_preview", lambda *a, **k: _learning(ready=False))

    result = optimizer.optimizer_preview(db, plan.id, settings=_settings(), as_of_at=AS_OF)

    assert result["optimizable_item_count"] == 1
    assert result["frozen_item_count"] == 3
    assert [r["item_id"] for r in result["recommendations"]] == ["planned"]
    assert {r["item_id"] for r in result["frozen_items"]} == {
        "generated", "scheduled", "planned-bound"
    }
    db.close(); engine.dispose()


def test_not_optimizer_ready_is_exploration_only(monkeypatch):
    engine, db = _db()
    plan = _seed_plan(db, item_specs=[
        {"id": "a"}, {"id": "b"}, {"id": "c"}
    ])
    monkeypatch.setattr(optimizer, "learning_preview", lambda *a, **k: _learning(ready=False))

    result = optimizer.optimizer_preview(db, plan.id, settings=_settings(), as_of_at=AS_OF)

    assert result["optimizer_ready"] is False
    assert result["exploit_count"] == 0
    assert result["explore_count"] == 3
    assert {r["selection_reason"] for r in result["recommendations"]} == {"EXPLORE"}
    db.close(); engine.dispose()


def test_exploit_explore_counts_are_deterministic(monkeypatch):
    engine, db = _db()
    plan = _seed_plan(db, item_specs=[{"id": f"i{i}"} for i in range(1, 11)])
    monkeypatch.setattr(optimizer, "learning_preview", lambda *a, **k: _learning(ready=True))

    result = optimizer.optimizer_preview(
        db, plan.id, settings=_settings(pinterest_optimizer_exploit_share=0.70), as_of_at=AS_OF
    )

    assert result["exploit_count"] == 7
    assert result["explore_count"] == 3
    assert sum(r["selection_reason"] == "EXPLOIT" for r in result["recommendations"]) == 7
    assert sum(r["selection_reason"] == "EXPLORE" for r in result["recommendations"]) == 3
    db.close(); engine.dispose()


def test_exact_dimension_weights_drive_evidence_score(monkeypatch):
    engine, db = _db()
    plan = _seed_plan(db, item_specs=[
        {"id": "a", "product_id": "p1", "board_id": "b1", "angle_id": "a1"},
        {"id": "b", "product_id": "p2", "board_id": "b2", "angle_id": "a2"},
    ])
    rankings = {
        "product": [_rank("p1", 1), _rank("p2", 2)],
        "board": [_rank("b2", 1), _rank("b1", 2)],
        "content_angle": [_rank("a1", 1), _rank("a2", 2)],
    }
    monkeypatch.setattr(
        optimizer, "learning_preview", lambda *a, **k: _learning(ready=True, rankings=rankings)
    )

    result = optimizer.optimizer_preview(
        db, plan.id, settings=_settings(pinterest_optimizer_exploit_share=0.50), as_of_at=AS_OF
    )
    rows = {r["item_id"]: r for r in result["recommendations"]}

    assert result["dimension_weights"] == {
        "product": "0.50", "board": "0.25", "content_angle": "0.25"
    }
    assert rows["a"]["evidence_score"] == "0.750000000000"
    assert rows["b"]["evidence_score"] == "0.250000000000"
    assert next(r for r in result["recommendations"] if r["selection_reason"] == "EXPLOIT")["item_id"] == "a"
    db.close(); engine.dispose()


def test_exploration_prefers_lower_historical_exposure(monkeypatch):
    engine, db = _db()
    plan = _seed_plan(db, item_specs=[
        {"id": "high", "product_id": "p-high", "board_id": "b-high", "angle_id": "a-high"},
        {"id": "low", "product_id": "p-low", "board_id": "b-low", "angle_id": "a-low"},
    ])
    rankings = {
        "product": [
            _rank("p-high", 1, publication_count=10),
            _rank("p-low", 2, publication_count=0),
        ],
        "board": [
            _rank("b-high", 1, publication_count=10),
            _rank("b-low", 2, publication_count=0),
        ],
        "content_angle": [
            _rank("a-high", 1, publication_count=10),
            _rank("a-low", 2, publication_count=0),
        ],
    }
    monkeypatch.setattr(
        optimizer, "learning_preview", lambda *a, **k: _learning(ready=True, rankings=rankings)
    )

    result = optimizer.optimizer_preview(
        db, plan.id, settings=_settings(pinterest_optimizer_exploit_share=0.0), as_of_at=AS_OF
    )

    assert [r["item_id"] for r in result["recommendations"]] == ["low", "high"]
    assert result["recommendations"][0]["historical_exposure"] == 0
    db.close(); engine.dispose()


def test_product_cap_violation_blocks_preview(monkeypatch):
    engine, db = _db()
    plan = _seed_plan(db, item_specs=[
        {"id": "a", "product_id": "same", "board_id": "b1", "angle_id": "a1"},
        {"id": "b", "product_id": "same", "board_id": "b2", "angle_id": "a2"},
        {"id": "c", "product_id": "same", "board_id": "b3", "angle_id": "a3"},
    ])
    monkeypatch.setattr(optimizer, "learning_preview", lambda *a, **k: _learning(ready=False))

    result = optimizer.optimizer_preview(
        db, plan.id,
        settings=_settings(pinterest_portfolio_max_pins_per_product=2),
        as_of_at=AS_OF,
    )
    assert result["ready"] is False
    assert "PRODUCT_CAP_EXCEEDED" in result["blockers"]
    db.close(); engine.dispose()


def test_board_share_violation_blocks_preview(monkeypatch):
    engine, db = _db()
    plan = _seed_plan(db, item_specs=[
        {"id": "a", "board_id": "same-board"},
        {"id": "b", "board_id": "same-board"},
        {"id": "c", "board_id": "other-board"},
    ])
    monkeypatch.setattr(optimizer, "learning_preview", lambda *a, **k: _learning(ready=False))

    result = optimizer.optimizer_preview(
        db, plan.id,
        settings=_settings(pinterest_portfolio_max_board_share=0.50),
        as_of_at=AS_OF,
    )
    assert result["ready"] is False
    assert "BOARD_SHARE_CAP_EXCEEDED" in result["blockers"]
    db.close(); engine.dispose()


def test_plan_store_is_passed_to_learning_and_other_plan_does_not_leak(monkeypatch):
    engine, db = _db()
    plan1 = _seed_plan(db, store_id="store-1", plan_id="plan-1", item_specs=[{"id": "a"}])
    plan2 = _seed_plan(db, store_id="store-2", plan_id="plan-2", item_specs=[{"id": "b"}])
    seen = []

    def fake_learning(db_arg, *, store_id, as_of_at, settings):
        seen.append(store_id)
        return _learning(ready=False, fingerprint=f"learn-{store_id}")

    monkeypatch.setattr(optimizer, "learning_preview", fake_learning)
    one = optimizer.optimizer_preview(db, plan1.id, settings=_settings(), as_of_at=AS_OF)
    two = optimizer.optimizer_preview(db, plan2.id, settings=_settings(), as_of_at=AS_OF)

    assert seen == ["store-1", "store-2"]
    assert [r["item_id"] for r in one["recommendations"]] == ["a"]
    assert [r["item_id"] for r in two["recommendations"]] == ["b"]
    assert one["learning_fingerprint"] != two["learning_fingerprint"]
    db.close(); engine.dispose()


def test_ties_and_fingerprint_are_stable(monkeypatch):
    engine, db = _db()
    plan = _seed_plan(db, item_specs=[
        {"id": "b", "slot_index": 2},
        {"id": "a", "slot_index": 1},
    ])
    monkeypatch.setattr(optimizer, "learning_preview", lambda *a, **k: _learning(ready=True))

    first = optimizer.optimizer_preview(db, plan.id, settings=_settings(), as_of_at=AS_OF)
    second = optimizer.optimizer_preview(db, plan.id, settings=_settings(), as_of_at=AS_OF)

    assert [r["item_id"] for r in first["recommendations"]] == [
        r["item_id"] for r in second["recommendations"]
    ]
    assert first["optimizer_fingerprint"] == second["optimizer_fingerprint"]
    db.close(); engine.dispose()


def test_preview_is_read_only_and_enabled_flag_does_not_apply(monkeypatch):
    engine, db = _db()
    plan = _seed_plan(db, item_specs=[{"id": "a"}, {"id": "b"}])
    monkeypatch.setattr(optimizer, "learning_preview", lambda *a, **k: _learning(ready=True))
    before = [
        (x.id, x.slot_index, x.planned_date, x.status, x.publication_id)
        for x in db.query(PinterestPortfolioPlanItem).order_by(PinterestPortfolioPlanItem.id)
    ]

    result = optimizer.optimizer_preview(
        db,
        plan.id,
        settings=_settings(pinterest_optimizer_enabled=True),
        as_of_at=AS_OF,
    )
    after = [
        (x.id, x.slot_index, x.planned_date, x.status, x.publication_id)
        for x in db.query(PinterestPortfolioPlanItem).order_by(PinterestPortfolioPlanItem.id)
    ]

    assert result["enabled"] is True
    assert result["state_mutated"] is False
    assert result["provider_called"] is False
    assert before == after
    assert len(db.new) == len(db.dirty) == len(db.deleted) == 0
    db.close(); engine.dispose()
