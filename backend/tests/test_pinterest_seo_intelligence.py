from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.models.domain import (
    Board,
    ContentAngle,
    KeywordCluster,
    PinConcept,
    PinDraft,
    PinterestPortfolioPlan,
    PinterestPortfolioPlanItem,
    PinterestSeoBrief,
    Product,
    ProductIntelligence,
    Store,
)
from app.services import pinterest_seo_intelligence as seo


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _settings(**overrides):
    values = {
        "database_url": "sqlite:///:memory:",
        "pinterest_seo_brief_persistence_enabled": False,
        "pinterest_seo_max_secondary_keywords": 5,
    }
    values.update(overrides)
    return Settings(**values)


def _seed(db):
    store = Store(
        id="store-1",
        name="Diamond Shelf",
        shop_domain="diamondshelf.us",
        market="US",
    )
    product = Product(
        id="product-1",
        store_id=store.id,
        shopify_product_id="1001",
        handle="afnan-9pm",
        title="Afnan 9PM Eau de Parfum",
        vendor="Afnan",
        product_type="Fragrance",
        product_url="https://diamondshelf.us/products/afnan-9pm",
        inventory_total=10,
        status="ACTIVE",
    )
    intelligence = ProductIntelligence(
        id="intel-1",
        product_id=product.id,
        brand="Afnan",
        audience="men",
        arabian_classification="ARABIAN",
        fragrance_family="Amber",
        fragrance_notes=["Vanilla", "Amber"],
        image_available=True,
        inventory_eligible=True,
        eligibility_status="ELIGIBLE",
        normalization_status="NORMALIZED",
    )
    board = Board(
        id="board-1",
        store_id=store.id,
        name="Arabian Fragrance",
        slug="arabian-fragrance",
        rules={},
        active=True,
    )
    angle = ContentAngle(
        id="angle-1",
        key="arabian-fragrance-discovery",
        name="Arabian Fragrance Discovery",
        description="Discover Arabian fragrances.",
        rules={},
        active=True,
    )
    cluster = KeywordCluster(
        id="cluster-1",
        key="arabian-fragrance",
        label="Arabian Fragrance",
        keywords=["Arabian Fragrance", "Middle Eastern Fragrance", "Arabian Perfume"],
        intent="discovery",
    )
    plan = PinterestPortfolioPlan(
        id="plan-1",
        store_id=store.id,
        month_start=date(2026, 9, 1),
        month_end=date(2026, 9, 30),
        target_pins=150,
        existing_commitments=0,
        planned_active_slots=1,
        reserve_slots=0,
        policy_version="PINTEREST_PORTFOLIO_V2",
        input_fingerprint="a" * 64,
        plan_fingerprint="b" * 64,
        status="DRAFT",
        metadata_json={},
    )
    item = PinterestPortfolioPlanItem(
        id="item-1",
        plan_id=plan.id,
        slot_index=1,
        is_reserve=False,
        planned_date=date(2026, 9, 21),
        product_id=product.id,
        local_board_id=board.id,
        board_key_snapshot=board.slug,
        content_angle_id=angle.id,
        angle_key_snapshot=angle.key,
        seed_keywords=["Arabian perfume", "Afnan perfume", "  ARABIAN---PERFUME  "],
        selection_score=100,
        selection_metadata={},
        item_fingerprint="c" * 64,
        status="PLANNED",
    )
    db.add_all([store, product, intelligence, board, angle, cluster, plan, item])
    db.commit()
    return {
        "store": store,
        "product": product,
        "intelligence": intelligence,
        "board": board,
        "angle": angle,
        "cluster": cluster,
        "plan": plan,
        "item": item,
    }


def test_keyword_normalization_and_deduplication():
    assert seo.normalize_keyword("  ARABIAN---Perfume!!! ") == "arabian perfume"
    assert seo.normalize_keyword("Vanilla\tPerfume") == "vanilla perfume"
    db = _db()
    seeded = _seed(db)
    result = seo.seo_brief_preview(db, seeded["item"].id, settings=_settings())
    assert result["ready"] is True
    assert len(result["source_evidence"]) == len(set(result["source_evidence"]))
    assert "arabian perfume" in result["source_evidence"]
    db.close()


def test_primary_keyword_selection_is_deterministic_and_board_angle_relevant():
    db = _db()
    item = _seed(db)["item"]

    first = seo.seo_brief_preview(db, item.id, settings=_settings())
    second = seo.seo_brief_preview(db, item.id, settings=_settings())

    assert first["primary_keyword"] == "arabian fragrance"
    assert first["primary_keyword"] == second["primary_keyword"]
    assert first["seo_fingerprint"] == second["seo_fingerprint"]
    assert first["dimension_scores"]["product_relevance"] > 0
    assert first["dimension_scores"]["board_fit"] > 0
    assert first["dimension_scores"]["angle_fit"] > 0
    db.close()


def test_keyword_cluster_intent_and_evidence_provenance_are_preserved():
    db = _db()
    item = _seed(db)["item"]
    result = seo.seo_brief_preview(db, item.id, settings=_settings())

    assert result["intent"] == "discovery"
    primary = result["source_evidence"][result["primary_keyword"]]
    assert "product_taxonomy" in primary["sources"]
    assert "keyword_cluster:arabian-fragrance" in primary["sources"]
    assert primary["intent"] == "discovery"
    db.close()


def test_secondary_keywords_are_bounded_and_semantically_distinct():
    db = _db()
    item = _seed(db)["item"]
    result = seo.seo_brief_preview(
        db,
        item.id,
        settings=_settings(pinterest_seo_max_secondary_keywords=3),
    )
    assert len(result["secondary_keywords"]) <= 3
    normalized = [seo.normalize_keyword(value) for value in result["secondary_keywords"]]
    assert len(normalized) == len(set(normalized))
    assert result["primary_keyword"] not in result["secondary_keywords"]
    db.close()


def test_score_semantics_do_not_claim_external_search_metrics():
    db = _db()
    item = _seed(db)["item"]
    result = seo.seo_brief_preview(db, item.id, settings=_settings())

    rendered = repr(result).lower()
    assert result["score_semantics"] == "deterministic_evidence_components_not_search_volume_or_rank_probability"
    for unsupported in ("search_volume", "cpc", "keyword_difficulty", "rank_probability"):
        assert unsupported not in result["dimension_scores"]
        assert unsupported not in result["source_evidence"]
    assert "provider_called" in result and result["provider_called"] is False
    assert "ai_called" in result and result["ai_called"] is False
    db.close()


def test_generation_coverage_targets_are_bounded_and_keyword_stuffing_is_prohibited():
    db = _db()
    item = _seed(db)["item"]
    result = seo.seo_brief_preview(db, item.id, settings=_settings())

    assert result["coverage_targets"]["title"]["max_characters"] == 100
    assert result["coverage_targets"]["description"]["max_characters"] == 500
    assert result["coverage_targets"]["title"]["must_include"] == [result["primary_keyword"]]
    assert result["coverage_targets"]["alt_text"]["keyword_stuffing_prohibited"] is True
    db.close()


def test_historical_primary_phrase_reuse_emits_cannibalization_warning():
    db = _db()
    seeded = _seed(db)
    concept = PinConcept(
        id="concept-history",
        store_id=seeded["store"].id,
        product_id=seeded["product"].id,
        content_angle_id=seeded["angle"].id,
        board_id=seeded["board"].id,
        fingerprint="d" * 64,
        rationale={},
    )
    draft = PinDraft(
        id="draft-history",
        concept_id=concept.id,
        version=1,
        title="Arabian Fragrance | Afnan 9PM",
        description="Explore this Arabian fragrance from Afnan.",
        alt_text="Afnan 9PM bottle",
        destination_url=seeded["product"].product_url,
        utm_url=seeded["product"].product_url + "?utm_source=pinterest",
        text_fingerprint="e" * 64,
    )
    db.add_all([concept, draft])
    db.commit()

    result = seo.seo_brief_preview(db, seeded["item"].id, settings=_settings())

    assert result["cannibalization_warnings"] == [{
        "code": "PRIMARY_KEYWORD_PREVIOUSLY_USED",
        "draft_id": draft.id,
        "same_board": True,
        "same_angle": True,
    }]
    db.close()


def test_missing_required_item_context_fails_closed():
    db = _db()
    seeded = _seed(db)
    db.delete(seeded["intelligence"])
    db.commit()

    result = seo.seo_brief_preview(db, seeded["item"].id, settings=_settings())
    assert result["ready"] is False
    assert result["blockers"] == ["PRODUCT_INTELLIGENCE_REQUIRED"]
    db.close()


def test_persistence_is_disabled_by_default():
    db = _db()
    item = _seed(db)["item"]
    with pytest.raises(seo.PinterestSeoError, match="PINTEREST_SEO_BRIEF_PERSISTENCE_DISABLED"):
        seo.persist_seo_brief(db, item.id, settings=_settings())
    assert db.query(PinterestSeoBrief).count() == 0
    db.close()


def test_exact_persistence_is_idempotent():
    db = _db()
    item = _seed(db)["item"]
    settings = _settings(pinterest_seo_brief_persistence_enabled=True)

    first = seo.persist_seo_brief(db, item.id, settings=settings)
    second = seo.persist_seo_brief(db, item.id, settings=settings)

    assert first.id == second.id
    assert first.primary_keyword == "arabian fragrance"
    assert db.query(PinterestSeoBrief).count() == 1
    db.close()


def test_persisted_brief_fails_closed_on_input_drift():
    db = _db()
    item = _seed(db)["item"]
    settings = _settings(pinterest_seo_brief_persistence_enabled=True)
    seo.persist_seo_brief(db, item.id, settings=settings)

    item.seed_keywords = ["men fragrance", "new fragrance"]
    db.commit()

    with pytest.raises(seo.PinterestSeoError, match="PINTEREST_SEO_BRIEF_INPUT_DRIFT"):
        seo.persist_seo_brief(db, item.id, settings=settings)
    assert db.query(PinterestSeoBrief).count() == 1
    db.close()


def test_preview_is_read_only_and_deterministic():
    db = _db()
    item = _seed(db)["item"]
    before = (len(db.new), len(db.dirty), len(db.deleted), db.query(PinterestSeoBrief).count())
    first = seo.seo_brief_preview(db, item.id, settings=_settings())
    second = seo.seo_brief_preview(db, item.id, settings=_settings())
    after = (len(db.new), len(db.dirty), len(db.deleted), db.query(PinterestSeoBrief).count())

    assert before == after
    assert first["input_fingerprint"] == second["input_fingerprint"]
    assert first["seo_fingerprint"] == second["seo_fingerprint"]
    assert first["state_mutated"] is False
    assert first["provider_called"] is False
    assert first["ai_called"] is False
    db.close()
