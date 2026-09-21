from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

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
from app.services import pinterest_adaptive_optimizer as optimizer
from app.services import pinterest_optimizer_apply as applysvc


POSTGRES_URL = os.getenv("TASK58_POSTGRES_URL")
AS_OF = datetime(2026, 9, 21, 18, 0, tzinfo=timezone.utc)


def _admin_url(url: str) -> str:
    return make_url(url).set(database="postgres").render_as_string(
        hide_password=False
    )


@pytest.fixture
def isolated_postgres() -> Iterator[str]:
    if not POSTGRES_URL:
        pytest.skip("TASK58_POSTGRES_URL is required")
    database = f"task584_{uuid4().hex[:16]}"
    admin = sa.create_engine(
        _admin_url(POSTGRES_URL),
        isolation_level="AUTOCOMMIT",
    )
    try:
        with admin.connect() as connection:
            connection.execute(sa.text(f'CREATE DATABASE "{database}"'))
        url = make_url(POSTGRES_URL).set(database=database).render_as_string(
            hide_password=False
        )
        engine = sa.create_engine(url)
        try:
            Base.metadata.create_all(engine)
        finally:
            engine.dispose()
        yield url
    finally:
        with admin.connect() as connection:
            connection.execute(
                sa.text(
                    "SELECT pg_terminate_backend(pid) "
                    "FROM pg_stat_activity "
                    "WHERE datname=:database AND pid <> pg_backend_pid()"
                ),
                {"database": database},
            )
            connection.execute(
                sa.text(f'DROP DATABASE IF EXISTS "{database}"')
            )
        admin.dispose()


def _settings() -> Settings:
    return Settings(
        database_url="sqlite+pysqlite:///:memory:",
        pinterest_optimizer_enabled=True,
        pinterest_optimizer_exploit_share=0.70,
        pinterest_portfolio_activation_enabled=True,
        pinterest_optimizer_apply_enabled=True,
        # Deliberately stricter than the frozen planner policy. These mutable
        # settings must not revoke a valid planner relaxation.
        pinterest_portfolio_max_pins_per_product=1,
        pinterest_portfolio_max_board_share=0.01,
        pinterest_learning_prior_impressions=500,
        pinterest_learning_min_total_publications=2,
        pinterest_learning_min_dimension_samples=2,
    )


def _learning(*args, **kwargs):
    return {
        "optimizer_ready": False,
        "learning_fingerprint": "pg-learning",
        "publication_count": 0,
        "rankings": {
            "product": [],
            "board": [],
            "content_angle": [],
        },
    }


def test_postgres_relaxed_board_contract_persists_and_applies(
    isolated_postgres: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = sa.create_engine(isolated_postgres)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    try:
        db.add(
            Store(
                id="store-pg",
                name="Diamond Shelf",
                shop_domain="diamondshelf.us",
            )
        )
        for index in range(1, 4):
            db.add(
                Product(
                    id=f"product-{index}",
                    store_id="store-pg",
                    shopify_product_id=f"shop-{index}",
                    handle=f"product-{index}",
                    title=f"Product {index}",
                    product_url=f"https://example.com/products/{index}",
                )
            )
            db.add(
                ContentAngle(
                    id=f"angle-{index}",
                    key=f"angle-{index}",
                    name=f"Angle {index}",
                    rules={},
                    active=True,
                )
            )
        db.add(
            Board(
                id="board-one",
                store_id="store-pg",
                name="Board One",
                slug="board-one",
                rules={},
                active=True,
            )
        )
        db.flush()

        cap_policy = {
            "max_pins_per_product": 3,
            "max_vendor_share": 1.0,
            "max_board_share": 0.33,
            "vendor_limit": 3,
            "board_limit": 1,
        }
        cap_relaxation = {
            "used": True,
            "vendor_cap_relaxed": False,
            "board_cap_relaxed": True,
            "vendor_limit": 3,
            "board_limit": 1,
        }
        plan = PinterestPortfolioPlan(
            id="plan-pg",
            store_id="store-pg",
            month_start=date(2026, 9, 1),
            month_end=date(2026, 9, 30),
            target_pins=3,
            existing_commitments=0,
            planned_active_slots=2,
            reserve_slots=1,
            policy_version="PINTEREST_PORTFOLIO_V2",
            input_fingerprint="a" * 64,
            plan_fingerprint="b" * 64,
            status="DRAFT",
            metadata_json={
                "cap_policy": cap_policy,
                "cap_relaxation": cap_relaxation,
            },
        )
        db.add(plan)
        db.flush()

        rows = [
            PinterestPortfolioPlanItem(
                id="item-1",
                plan_id=plan.id,
                slot_index=1,
                is_reserve=False,
                planned_date=date(2026, 9, 22),
                product_id="product-1",
                local_board_id="board-one",
                board_key_snapshot="board-one",
                content_angle_id="angle-1",
                angle_key_snapshot="angle-1",
                seed_keywords=[],
                selection_score=Decimal("3.0"),
                selection_metadata={
                    "candidate_fingerprint": "1" * 64,
                    "vendor_key": "vendor",
                    "selection_stage": "STRICT",
                    "relaxed_vendor_cap": False,
                    "relaxed_board_cap": False,
                },
                item_fingerprint="1" * 64,
                status="PLANNED",
            ),
            PinterestPortfolioPlanItem(
                id="item-2",
                plan_id=plan.id,
                slot_index=2,
                is_reserve=False,
                planned_date=date(2026, 9, 23),
                product_id="product-2",
                local_board_id="board-one",
                board_key_snapshot="board-one",
                content_angle_id="angle-2",
                angle_key_snapshot="angle-2",
                seed_keywords=[],
                selection_score=Decimal("2.0"),
                selection_metadata={
                    "candidate_fingerprint": "2" * 64,
                    "vendor_key": "vendor",
                    "selection_stage": "RELAXED",
                    "relaxed_vendor_cap": False,
                    "relaxed_board_cap": True,
                },
                item_fingerprint="2" * 64,
                status="PLANNED",
            ),
            PinterestPortfolioPlanItem(
                id="item-3",
                plan_id=plan.id,
                slot_index=3,
                is_reserve=True,
                planned_date=None,
                product_id="product-3",
                local_board_id="board-one",
                board_key_snapshot="board-one",
                content_angle_id="angle-3",
                angle_key_snapshot="angle-3",
                seed_keywords=[],
                selection_score=Decimal("1.0"),
                selection_metadata={
                    "candidate_fingerprint": "3" * 64,
                    "vendor_key": "vendor",
                    "selection_stage": "RESERVE",
                    "relaxed_vendor_cap": False,
                    "relaxed_board_cap": False,
                },
                item_fingerprint="3" * 64,
                status="PLANNED",
            ),
        ]
        db.add_all(rows)
        db.commit()

        monkeypatch.setattr(optimizer, "learning_preview", _learning)
        settings = _settings()
        preview = optimizer.optimizer_preview(
            db,
            plan.id,
            settings=settings,
            as_of_at=AS_OF,
        )
        assert preview["ready"] is True
        assert preview["planner_cap_contract"]["board_cap_relaxed"] is True
        assert "BOARD_SHARE_CAP_EXCEEDED" not in preview["blockers"]

        readiness = applysvc.optimizer_apply_readiness(
            db,
            plan.id,
            settings=settings,
            as_of_at=AS_OF,
        )
        assert readiness["ready"] is True

        application = applysvc.apply_optimizer(
            db,
            plan.id,
            expected_optimizer_fingerprint=readiness[
                "optimizer_fingerprint"
            ],
            expected_input_state_fingerprint=readiness[
                "input_state_fingerprint"
            ],
            settings=settings,
            as_of_at=AS_OF,
            now=AS_OF,
        )
        db.expire_all()

        assert db.get(PinterestPortfolioPlan, plan.id).status == "ACTIVE"
        persisted = db.get(PinterestPortfolioPlan, plan.id).metadata_json
        assert persisted["cap_relaxation"]["board_cap_relaxed"] is True
        assert application.status == "APPLIED"
        assert db.query(PinterestOptimizerApplication).count() == 1
        assert (
            application.recommendation_snapshot[
                "planner_cap_contract_fingerprint"
            ]
            == readiness["planner_cap_contract_fingerprint"]
        )
    finally:
        db.close()
        engine.dispose()
