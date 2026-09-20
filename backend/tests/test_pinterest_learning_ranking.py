from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.db.base import Base
from app.models.domain import (
    Board,
    ContentAngle,
    CreativeTemplate,
    PinConcept,
    PinCreative,
    PinDraft,
    PinPublication,
    PinterestAnalyticsSnapshot,
    PinterestLearningSnapshot,
    PinterestPortfolioPlan,
    PinterestPortfolioPlanItem,
    PinterestSeoBrief,
    Product,
    PublicationStatus,
    Store,
)
from app.services import pinterest_learning_ranking as learning
from app.services.pinterest_performance_analytics import METRIC_POLICY_VERSION


AS_OF = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


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
        "pinterest_learning_snapshot_persistence_enabled": False,
        "pinterest_learning_prior_impressions": 500,
        "pinterest_learning_min_total_publications": 2,
        "pinterest_learning_min_dimension_samples": 2,
    }
    values.update(overrides)
    return Settings(**values)


def _seed_identity(
    db,
    *,
    suffix,
    store_id="store-1",
    product_id=None,
    board_id=None,
    angle_id=None,
    template_id=None,
    keyword="arabian perfume",
    intent="product_discovery",
):
    if db.get(Store, store_id) is None:
        db.add(Store(id=store_id, name=store_id, shop_domain=f"{store_id}.example"))
        db.flush()

    product_id = product_id or f"product-{suffix}"
    if db.get(Product, product_id) is None:
        db.add(Product(
            id=product_id,
            store_id=store_id,
            shopify_product_id=f"shopify-{suffix}",
            handle=f"product-{suffix}",
            title=f"Product {suffix}",
            product_url=f"https://example.com/products/{suffix}",
        ))
        db.flush()

    board_id = board_id or f"board-{suffix}"
    if db.get(Board, board_id) is None:
        db.add(Board(
            id=board_id,
            store_id=store_id,
            name=f"Board {suffix}",
            slug=f"board-{suffix}",
            rules={},
            active=True,
        ))
        db.flush()

    angle_id = angle_id or f"angle-{suffix}"
    if db.get(ContentAngle, angle_id) is None:
        db.add(ContentAngle(
            id=angle_id,
            key=f"angle-{suffix}",
            name=f"Angle {suffix}",
            rules={},
            active=True,
        ))
        db.flush()

    template_id = template_id or f"template-{suffix}"
    if db.get(CreativeTemplate, template_id) is None:
        db.add(CreativeTemplate(
            id=template_id,
            key=f"template-{suffix}",
            version=1,
            name=f"Template {suffix}",
            renderer="satori",
            definition={},
            active=True,
        ))
        db.flush()

    concept = PinConcept(
        id=f"concept-{suffix}",
        store_id=store_id,
        product_id=product_id,
        content_angle_id=angle_id,
        board_id=board_id,
        fingerprint=(f"{suffix}c" + "0" * 64)[:64],
        rationale={},
    )
    draft = PinDraft(
        id=f"draft-{suffix}",
        concept_id=concept.id,
        version=1,
        title=f"Draft {suffix}",
        description=f"Description {suffix}",
        alt_text=f"Alt {suffix}",
        destination_url=f"https://example.com/{suffix}",
        utm_url=f"https://example.com/{suffix}?utm_source=pinterest",
        text_fingerprint=(f"{suffix}d" + "1" * 64)[:64],
    )
    creative = PinCreative(
        id=f"creative-{suffix}",
        draft_id=draft.id,
        template_id=template_id,
        creative_fingerprint=(f"{suffix}e" + "2" * 64)[:64],
        render_status="RENDERED",
        width=1000,
        height=1500,
    )
    publication = PinPublication(
        id=f"pub-{suffix}",
        draft_id=draft.id,
        creative_id=creative.id,
        template_id=template_id,
        template_key=f"template-{suffix}",
        board_id=board_id,
        creative_fingerprint=creative.creative_fingerprint,
        publication_fingerprint=(f"{suffix}p" + "3" * 64)[:64],
        status=PublicationStatus.PUBLISHED,
        pinterest_pin_id=f"pin-{suffix}",
        published_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    plan = PinterestPortfolioPlan(
        id=f"plan-{suffix}",
        store_id=store_id,
        month_start=date(2026, 8, 1),
        month_end=date(2026, 8, 31),
        target_pins=150,
        existing_commitments=0,
        planned_active_slots=1,
        reserve_slots=0,
        policy_version="PORTFOLIO_V1",
        input_fingerprint=(f"{suffix}i" + "4" * 64)[:64],
        plan_fingerprint=(f"{suffix}q" + "5" * 64)[:64],
        status="COMPLETED",
        metadata_json={},
    )
    item = PinterestPortfolioPlanItem(
        id=f"item-{suffix}",
        plan_id=plan.id,
        slot_index=1,
        is_reserve=False,
        planned_date=date(2026, 8, 1),
        product_id=product_id,
        local_board_id=board_id,
        board_key_snapshot=f"board-{suffix}",
        content_angle_id=angle_id,
        angle_key_snapshot=f"angle-{suffix}",
        seed_keywords=[keyword],
        selection_score=Decimal("1.000000"),
        selection_metadata={},
        item_fingerprint=(f"{suffix}s" + "6" * 64)[:64],
        status="PUBLISHED",
        publication_id=publication.id,
    )
    seo = PinterestSeoBrief(
        id=f"seo-{suffix}",
        portfolio_item_id=item.id,
        policy_version="SEO_V1",
        input_fingerprint=(f"{suffix}u" + "7" * 64)[:64],
        seo_fingerprint=(f"{suffix}v" + "8" * 64)[:64],
        primary_keyword=keyword,
        secondary_keywords=[],
        intent=intent,
        source_evidence={},
        dimension_scores={},
        coverage_targets={},
        guidance={},
        cannibalization_warnings=[],
        status="CURRENT",
    )
    db.add_all([concept, draft, creative, publication, plan, item, seo])
    db.commit()
    return publication


def _snapshot(
    db,
    publication,
    *,
    window="D7",
    impressions=100,
    saves=10,
    pin_clicks=5,
    outbound_clicks=2,
    engagements=20,
    finalized_at=AS_OF,
):
    suffix = f"{publication.id}-{window}"
    span = {"D1": 1, "D7": 7, "D30": 30, "D90": 90}[window]
    row = PinterestAnalyticsSnapshot(
        id=f"snap-{suffix}",
        publication_id=publication.id,
        pinterest_pin_id=publication.pinterest_pin_id,
        metric_policy_version=METRIC_POLICY_VERSION,
        observation_window=window,
        range_start=date(2026, 8, 1),
        range_end=date(2026, 8, span),
        provider_payload_fingerprint=(suffix.replace("-", "") + "f" * 64)[:64],
        impressions=impressions,
        saves=saves,
        pin_clicks=pin_clicks,
        outbound_clicks=outbound_clicks,
        engagements=engagements,
        save_rate=Decimal(saves) / Decimal(impressions) if impressions else Decimal("0"),
        pin_click_rate=Decimal(pin_clicks) / Decimal(impressions) if impressions else Decimal("0"),
        outbound_click_rate=Decimal(outbound_clicks) / Decimal(impressions) if impressions else Decimal("0"),
        engagement_rate=Decimal(engagements) / Decimal(impressions) if impressions else Decimal("0"),
        safe_metric_map={},
        observed_at=finalized_at,
        finalized_at=finalized_at,
    )
    db.add(row)
    db.commit()
    return row


def test_latest_matured_window_selected_without_cumulative_double_counting():
    engine, db = _db()
    pub = _seed_identity(db, suffix="a")
    _snapshot(db, pub, window="D1", impressions=10, outbound_clicks=1)
    _snapshot(db, pub, window="D7", impressions=100, outbound_clicks=5)

    preview = learning.learning_preview(
        db,
        store_id="store-1",
        as_of_at=AS_OF,
        settings=_settings(),
    )

    assert preview["publication_count"] == 1
    assert preview["snapshot_count"] == 1
    assert preview["global_priors"]["counts"]["impressions"] == 100
    assert preview["global_priors"]["counts"]["outbound_clicks"] == 5
    assert preview["selected_window_distribution"] == {
        "D1": 0, "D7": 1, "D30": 0, "D90": 0
    }
    db.close(); engine.dispose()


def test_as_of_excludes_future_finalized_snapshot():
    engine, db = _db()
    pub = _seed_identity(db, suffix="a")
    _snapshot(db, pub, window="D7", impressions=100, finalized_at=AS_OF)
    _snapshot(
        db,
        pub,
        window="D30",
        impressions=999,
        finalized_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )

    preview = learning.learning_preview(
        db,
        store_id="store-1",
        as_of_at=AS_OF,
        settings=_settings(),
    )
    assert preview["global_priors"]["counts"]["impressions"] == 100
    assert preview["selected_window_distribution"]["D7"] == 1
    db.close(); engine.dispose()


def test_pooled_counts_priors_and_exact_composite_weights():
    engine, db = _db()
    p1 = _seed_identity(db, suffix="a", product_id="product-shared")
    p2 = _seed_identity(
        db,
        suffix="b",
        product_id="product-shared",
        board_id="board-a",
        angle_id="angle-a",
        template_id="template-a",
        keyword="arabian perfume",
        intent="product_discovery",
    )
    _snapshot(
        db, p1, impressions=100, saves=10, pin_clicks=20,
        outbound_clicks=30, engagements=40
    )
    _snapshot(
        db, p2, impressions=300, saves=30, pin_clicks=30,
        outbound_clicks=15, engagements=60
    )

    preview = learning.learning_preview(
        db,
        store_id="store-1",
        as_of_at=AS_OF,
        settings=_settings(pinterest_learning_prior_impressions=500),
    )
    counts = preview["global_priors"]["counts"]
    assert counts == {
        "impressions": 400,
        "saves": 40,
        "pin_clicks": 50,
        "outbound_clicks": 45,
        "engagements": 100,
    }
    assert preview["global_priors"]["rates"]["save_rate"] == "0.100000000000"

    row = preview["rankings"]["product"][0]
    assert row["publication_count"] == 2
    expected = (
        Decimal(row["smoothed_rates"]["outbound_click_rate"]) * Decimal("0.40")
        + Decimal(row["smoothed_rates"]["save_rate"]) * Decimal("0.25")
        + Decimal(row["smoothed_rates"]["pin_click_rate"]) * Decimal("0.20")
        + Decimal(row["smoothed_rates"]["engagement_rate"]) * Decimal("0.15")
    ).quantize(Decimal("0.000000000001"))
    assert Decimal(row["score"]) == expected
    db.close(); engine.dispose()


def test_low_exposure_rate_is_shrunk_toward_global_prior():
    engine, db = _db()
    high = _seed_identity(db, suffix="high")
    low = _seed_identity(db, suffix="low")
    _snapshot(db, high, impressions=1000, saves=100, outbound_clicks=100)
    _snapshot(db, low, impressions=10, saves=10, outbound_clicks=10)

    preview = learning.learning_preview(
        db,
        store_id="store-1",
        as_of_at=AS_OF,
        settings=_settings(pinterest_learning_prior_impressions=500),
    )
    low_row = next(
        row for row in preview["rankings"]["product"]
        if row["entity_key"] == "product-low"
    )
    assert Decimal(low_row["raw_rates"]["save_rate"]) == Decimal("1")
    assert Decimal(low_row["smoothed_rates"]["save_rate"]) < Decimal("1")
    assert Decimal(low_row["smoothed_rates"]["save_rate"]) > Decimal("0")
    assert Decimal(low_row["confidence"]) < Decimal("0.1")
    db.close(); engine.dispose()


def test_zero_impressions_are_safe_and_deterministic():
    engine, db = _db()
    pub = _seed_identity(db, suffix="zero")
    _snapshot(
        db, pub, impressions=0, saves=0, pin_clicks=0,
        outbound_clicks=0, engagements=0
    )
    first = learning.learning_preview(
        db, store_id="store-1", as_of_at=AS_OF, settings=_settings()
    )
    second = learning.learning_preview(
        db, store_id="store-1", as_of_at=AS_OF, settings=_settings()
    )
    row = first["rankings"]["product"][0]
    assert all(v == "0.000000000000" for v in row["raw_rates"].values())
    assert row["score"] == "0.000000000000"
    assert first["input_fingerprint"] == second["input_fingerprint"]
    assert first["learning_fingerprint"] == second["learning_fingerprint"]
    db.close(); engine.dispose()


def test_all_persisted_lineage_dimensions_are_ranked():
    engine, db = _db()
    pub = _seed_identity(
        db,
        suffix="a",
        keyword="long lasting arabian perfume",
        intent="commercial_discovery",
    )
    _snapshot(db, pub)

    preview = learning.learning_preview(
        db, store_id="store-1", as_of_at=AS_OF, settings=_settings()
    )
    assert preview["rankings"]["product"][0]["entity_key"] == "product-a"
    assert preview["rankings"]["board"][0]["entity_key"] == "board-a"
    assert preview["rankings"]["content_angle"][0]["entity_key"] == "angle-a"
    assert preview["rankings"]["template"][0]["entity_key"] == "template-a"
    assert preview["rankings"]["creative"][0]["entity_key"] == "creative-a"
    assert preview["rankings"]["primary_keyword"][0]["entity_key"] == "long lasting arabian perfume"
    assert preview["rankings"]["intent"][0]["entity_key"] == "commercial_discovery"
    db.close(); engine.dispose()


def test_missing_seo_lineage_omits_only_keyword_and_intent_dimensions():
    engine, db = _db()
    pub = _seed_identity(db, suffix="a")
    db.query(PinterestSeoBrief).delete()
    db.commit()
    _snapshot(db, pub)

    preview = learning.learning_preview(
        db, store_id="store-1", as_of_at=AS_OF, settings=_settings()
    )
    assert preview["rankings"]["product"]
    assert preview["rankings"]["board"]
    assert preview["rankings"]["primary_keyword"] == []
    assert preview["rankings"]["intent"] == []
    db.close(); engine.dispose()


def test_store_isolation_filters_other_store_evidence():
    engine, db = _db()
    p1 = _seed_identity(db, suffix="a", store_id="store-1")
    p2 = _seed_identity(db, suffix="b", store_id="store-2")
    _snapshot(db, p1, impressions=100)
    _snapshot(db, p2, impressions=900)

    one = learning.learning_preview(
        db, store_id="store-1", as_of_at=AS_OF, settings=_settings()
    )
    two = learning.learning_preview(
        db, store_id="store-2", as_of_at=AS_OF, settings=_settings()
    )
    assert one["publication_count"] == 1
    assert one["global_priors"]["counts"]["impressions"] == 100
    assert two["global_priors"]["counts"]["impressions"] == 900
    assert {r["entity_key"] for r in one["rankings"]["product"]} == {"product-a"}
    assert {r["entity_key"] for r in two["rankings"]["product"]} == {"product-b"}
    db.close(); engine.dispose()


def test_thresholds_control_optimizer_and_exploitation_readiness():
    engine, db = _db()
    p1 = _seed_identity(db, suffix="a", product_id="product-shared")
    p2 = _seed_identity(db, suffix="b", product_id="product-shared")
    _snapshot(db, p1)
    _snapshot(db, p2)

    below = learning.learning_preview(
        db,
        store_id="store-1",
        as_of_at=AS_OF,
        settings=_settings(
            pinterest_learning_min_total_publications=3,
            pinterest_learning_min_dimension_samples=2,
        ),
    )
    assert below["optimizer_ready"] is False
    assert below["rankings"]["product"][0]["exploitation_eligible"] is False

    ready = learning.learning_preview(
        db,
        store_id="store-1",
        as_of_at=AS_OF,
        settings=_settings(
            pinterest_learning_min_total_publications=2,
            pinterest_learning_min_dimension_samples=2,
        ),
    )
    assert ready["optimizer_ready"] is True
    assert ready["rankings"]["product"][0]["exploitation_eligible"] is True
    db.close(); engine.dispose()


def test_stable_tie_breaking_uses_entity_key():
    engine, db = _db()
    p1 = _seed_identity(db, suffix="b")
    p2 = _seed_identity(db, suffix="a")
    _snapshot(db, p1, impressions=100, saves=10, pin_clicks=10, outbound_clicks=10, engagements=10)
    _snapshot(db, p2, impressions=100, saves=10, pin_clicks=10, outbound_clicks=10, engagements=10)

    preview = learning.learning_preview(
        db, store_id="store-1", as_of_at=AS_OF, settings=_settings()
    )
    assert [r["entity_key"] for r in preview["rankings"]["product"]] == [
        "product-a", "product-b"
    ]
    assert [r["rank"] for r in preview["rankings"]["product"]] == [1, 2]
    db.close(); engine.dispose()


def test_preview_is_read_only_and_provider_free(monkeypatch):
    engine, db = _db()
    pub = _seed_identity(db, suffix="a")
    _snapshot(db, pub)
    before = (len(db.new), len(db.dirty), len(db.deleted), db.query(PinterestLearningSnapshot).count())

    called = []
    def forbidden(*args, **kwargs):
        called.append(1)
        raise AssertionError("provider access forbidden")
    monkeypatch.setattr(
        "app.services.pinterest_performance_analytics.PinterestAnalyticsClient.fetch_pin_analytics",
        forbidden,
    )

    result = learning.learning_preview(
        db, store_id="store-1", as_of_at=AS_OF, settings=_settings()
    )
    after = (len(db.new), len(db.dirty), len(db.deleted), db.query(PinterestLearningSnapshot).count())
    assert result["provider_called"] is False
    assert result["state_mutated"] is False
    assert before == after
    assert called == []
    db.close(); engine.dispose()


def test_persistence_disabled_by_default_and_idempotent_when_enabled():
    engine, db = _db()
    pub = _seed_identity(db, suffix="a")
    _snapshot(db, pub)

    with pytest.raises(learning.LearningError, match="LEARNING_SNAPSHOT_PERSISTENCE_DISABLED"):
        learning.persist_learning_snapshot(
            db, store_id="store-1", as_of_at=AS_OF, settings=_settings()
        )

    enabled = _settings(pinterest_learning_snapshot_persistence_enabled=True)
    first = learning.persist_learning_snapshot(
        db, store_id="store-1", as_of_at=AS_OF, settings=enabled
    )
    second = learning.persist_learning_snapshot(
        db, store_id="store-1", as_of_at=AS_OF, settings=enabled
    )
    assert first["status"] == "CREATED"
    assert second["status"] == "IDEMPOTENT"
    assert second["snapshot_id"] == first["snapshot_id"]
    assert db.query(PinterestLearningSnapshot).count() == 1
    db.close(); engine.dispose()


def test_changed_evidence_creates_new_immutable_learning_snapshot():
    engine, db = _db()
    p1 = _seed_identity(db, suffix="a")
    _snapshot(db, p1)
    enabled = _settings(pinterest_learning_snapshot_persistence_enabled=True)
    first = learning.persist_learning_snapshot(
        db, store_id="store-1", as_of_at=AS_OF, settings=enabled
    )

    p2 = _seed_identity(db, suffix="b")
    _snapshot(db, p2)
    second = learning.persist_learning_snapshot(
        db, store_id="store-1", as_of_at=AS_OF, settings=enabled
    )

    assert first["snapshot_id"] != second["snapshot_id"]
    assert first["learning_fingerprint"] != second["learning_fingerprint"]
    assert db.query(PinterestLearningSnapshot).count() == 2
    db.close(); engine.dispose()
