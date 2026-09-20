from datetime import date, datetime, timezone
import hashlib

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.models.domain import (
    Board,
    ContentAngle,
    CreativeTemplate,
    DraftStatus,
    PinConcept,
    PinCreative,
    PinDraft,
    PinPublication,
    PinterestAutonomousGenerationRun,
    PinterestBoard,
    PinterestConnection,
    PinterestPortfolioPlan,
    PinterestPortfolioPlanItem,
    PinterestSeoBrief,
    Product,
    ProductImage,
    ProductIntelligence,
    Store,
)
from app.services import pinterest_autonomous_generation as generation


NOW = datetime(2026, 9, 20, 13, 0, tzinfo=timezone.utc)


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _settings(**overrides):
    values = {
        "database_url": "sqlite:///:memory:",
        "pinterest_autonomous_generation_enabled": False,
        "pinterest_board_write_scope_enabled": False,
        "pinterest_board_provisioning_enabled": False,
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
        status="ACTIVE",
        product_url="https://diamondshelf.us/products/afnan-9pm",
        inventory_total=12,
    )
    intelligence = ProductIntelligence(
        id="intel-1",
        product_id=product.id,
        brand="Afnan",
        audience="men",
        arabian_classification="ARABIAN",
        fragrance_family="Amber",
        fragrance_notes=["Vanilla"],
        image_available=True,
        inventory_eligible=True,
        eligibility_status="ELIGIBLE",
        normalization_status="NORMALIZED",
    )
    image = ProductImage(
        id="image-1",
        product_id=product.id,
        shopify_media_id="media-1",
        source_url="https://cdn.shopify.com/s/files/1/0000/afnan.png",
        source_sha256="a" * 64,
        is_primary=True,
        editorial_eligible=True,
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
        input_fingerprint="b" * 64,
        plan_fingerprint="c" * 64,
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
        seed_keywords=["arabian fragrance", "afnan perfume"],
        selection_score=100,
        selection_metadata={},
        item_fingerprint="d" * 64,
        status="PLANNED",
    )
    seo = PinterestSeoBrief(
        id="seo-1",
        portfolio_item_id=item.id,
        policy_version="PINTEREST_SEO_BRIEF_V1",
        input_fingerprint="e" * 64,
        seo_fingerprint="f" * 64,
        primary_keyword="arabian fragrance",
        secondary_keywords=["afnan perfume"],
        intent="discovery",
        source_evidence={},
        dimension_scores={},
        coverage_targets={
            "title": {"must_include": ["arabian fragrance"]},
            "description": {"must_include": ["arabian fragrance"]},
        },
        guidance={},
        cannibalization_warnings=[],
        status="CURRENT",
    )
    connection = PinterestConnection(
        id="connection-1",
        external_user_id="user-1",
        username="diamond-shelf",
        granted_scopes=["user_accounts:read", "boards:read", "pins:read"],
        access_token_ciphertext="cipher-a",
        refresh_token_ciphertext="cipher-r",
        status="CONNECTED",
        boards_last_synced_at=NOW,
    )
    provider_board = PinterestBoard(
        id="provider-board-row-1",
        connection_id=connection.id,
        external_board_id="provider-board-1",
        name="Arabian Fragrance",
        privacy="PUBLIC",
        is_active=True,
        is_eligible=True,
        routing_label="arabian-fragrance",
        last_seen_at=NOW,
        last_synced_at=NOW,
    )
    db.add_all([
        store,
        product,
        intelligence,
        image,
        board,
        angle,
        plan,
        item,
        seo,
        connection,
        provider_board,
    ])
    db.commit()
    return {
        "store": store,
        "product": product,
        "intelligence": intelligence,
        "image": image,
        "board": board,
        "angle": angle,
        "plan": plan,
        "item": item,
        "seo": seo,
        "connection": connection,
        "provider_board": provider_board,
    }


class FakeRenderer:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def render_variant(self, draft_id, template_key, *, db=None):
        self.calls.append((draft_id, template_key))
        if self.fail:
            return {"draft_id": draft_id, "status": "FAILED", "error": "synthetic"}
        template = db.scalar(
            select(CreativeTemplate)
            .where(CreativeTemplate.key == template_key, CreativeTemplate.version == 1)
        )
        if template is None:
            template = CreativeTemplate(
                id="template-1",
                key=template_key,
                version=1,
                name=template_key.replace("_", " ").title(),
                renderer="pillow",
                definition={},
                active=True,
            )
            db.add(template)
            db.flush()
        draft = db.get(PinDraft, draft_id)
        concept = db.get(PinConcept, draft.concept_id)
        image = db.scalar(
            select(ProductImage)
            .where(ProductImage.product_id == concept.product_id)
            .limit(1)
        )
        fp = hashlib.sha256(f"{draft_id}:{template_key}".encode()).hexdigest()
        creative = PinCreative(
            id=f"creative-{draft_id}",
            draft_id=draft_id,
            template_id=template.id,
            source_image_id=image.id,
            rendered_url=f"/api/pins/public-creatives/{draft_id}.png",
            sha256="9" * 64,
            creative_fingerprint=fp,
            width=1000,
            height=1500,
            render_status="RENDERED",
            render_spec={"synthetic": True},
            rendered_at=NOW,
        )
        db.add(creative)
        db.flush()
        return {
            "draft_id": draft_id,
            "creative_id": creative.id,
            "status": "RENDERED",
            "image_url": creative.rendered_url,
        }


def test_generation_readiness_happy_path_binds_seo_board_image_and_template():
    db = _db()
    seeded = _seed(db)

    result = generation.autonomous_generation_readiness(
        db,
        seeded["item"].id,
        settings=_settings(),
    )

    assert result["ready"] is True
    assert result["blockers"] == []
    assert result["seo_brief_id"] == seeded["seo"].id
    assert result["seo_fingerprint"] == seeded["seo"].seo_fingerprint
    assert result["selected_board_record_id"] == seeded["provider_board"].id
    assert result["selected_external_board_id"] == seeded["provider_board"].external_board_id
    assert result["source_image_id"] == seeded["image"].id
    assert result["template_key"] == "product_classification"
    assert "arabian fragrance" in generation.normalize_keyword(
        f"{result['copy']['title']} {result['copy']['description']}"
    )
    assert len(result["input_fingerprint"]) == 64
    assert result["state_mutated"] is False
    assert result["provider_called"] is False
    assert result["ai_called"] is False
    db.close()


def test_current_seo_brief_is_required():
    db = _db()
    seeded = _seed(db)
    db.delete(seeded["seo"])
    db.commit()

    result = generation.autonomous_generation_readiness(
        db,
        seeded["item"].id,
        settings=_settings(),
    )

    assert result["ready"] is False
    assert "CURRENT_SEO_BRIEF_REQUIRED" in result["blockers"]
    db.close()


def test_routable_existing_pinterest_board_is_required():
    db = _db()
    seeded = _seed(db)
    seeded["provider_board"].is_eligible = False
    db.commit()

    result = generation.autonomous_generation_readiness(
        db,
        seeded["item"].id,
        settings=_settings(),
    )

    assert result["ready"] is False
    assert "ROUTABLE_PINTEREST_BOARD_REQUIRED" in result["blockers"]
    db.close()


def test_exact_authentic_editorial_image_is_required():
    db = _db()
    seeded = _seed(db)
    seeded["image"].editorial_eligible = False
    db.commit()

    result = generation.autonomous_generation_readiness(
        db,
        seeded["item"].id,
        settings=_settings(),
    )

    assert result["ready"] is False
    assert "AUTHENTIC_IMAGE_REQUIRED" in result["blockers"]
    db.close()


def test_unsupported_seo_claim_blocks_generation():
    db = _db()
    seeded = _seed(db)
    seeded["seo"].primary_keyword = "best arabian fragrance"
    db.commit()

    result = generation.autonomous_generation_readiness(
        db,
        seeded["item"].id,
        settings=_settings(),
    )

    assert result["ready"] is False
    assert "UNSUPPORTED_CLAIM_DETECTED" in result["blockers"]
    db.close()


def test_execution_is_disabled_by_default():
    db = _db()
    seeded = _seed(db)

    with pytest.raises(generation.AutonomousGenerationError, match="AUTONOMOUS_GENERATION_DISABLED"):
        generation.execute_autonomous_generation(
            db,
            seeded["item"].id,
            settings=_settings(),
            renderer=FakeRenderer(),
            now=NOW,
        )

    assert db.query(PinterestAutonomousGenerationRun).count() == 0
    assert db.query(PinConcept).count() == 0
    assert db.query(PinDraft).count() == 0
    db.close()


def test_successful_generation_creates_exact_lineage_and_stops_before_authorization():
    db = _db()
    seeded = _seed(db)
    renderer = FakeRenderer()
    settings = _settings(pinterest_autonomous_generation_enabled=True)

    run = generation.execute_autonomous_generation(
        db,
        seeded["item"].id,
        settings=settings,
        renderer=renderer,
        now=NOW,
    )

    assert run.status == "SUCCEEDED"
    assert run.seo_brief_id == seeded["seo"].id
    assert run.concept_id and run.draft_id and run.creative_id
    concept = db.get(PinConcept, run.concept_id)
    draft = db.get(PinDraft, run.draft_id)
    creative = db.get(PinCreative, run.creative_id)
    assert concept.rationale["portfolio_item_id"] == seeded["item"].id
    assert concept.rationale["seo_fingerprint"] == seeded["seo"].seo_fingerprint
    assert concept.rationale["board_mapping"]["pinterest_board_record_id"] == seeded["provider_board"].id
    assert draft.status == DraftStatus.READY_FOR_REVIEW
    assert draft.destination_url == seeded["product"].product_url
    assert creative.draft_id == draft.id
    assert creative.render_status == "RENDERED"
    assert db.get(PinterestPortfolioPlanItem, seeded["item"].id).status == "GENERATED"
    assert db.query(PinPublication).count() == 0
    assert renderer.calls == [(draft.id, "product_classification")]
    db.close()


def test_exact_successful_generation_is_idempotent():
    db = _db()
    seeded = _seed(db)
    renderer = FakeRenderer()
    settings = _settings(pinterest_autonomous_generation_enabled=True)

    first = generation.execute_autonomous_generation(
        db,
        seeded["item"].id,
        settings=settings,
        renderer=renderer,
        now=NOW,
    )
    second = generation.execute_autonomous_generation(
        db,
        seeded["item"].id,
        settings=settings,
        renderer=renderer,
        now=NOW,
    )

    assert first.id == second.id
    assert db.query(PinterestAutonomousGenerationRun).count() == 1
    assert db.query(PinConcept).count() == 1
    assert db.query(PinDraft).count() == 1
    assert len(renderer.calls) == 1
    db.close()


def test_started_boundary_blocks_second_execution():
    db = _db()
    seeded = _seed(db)
    readiness = generation.autonomous_generation_readiness(
        db,
        seeded["item"].id,
        settings=_settings(),
    )
    run = PinterestAutonomousGenerationRun(
        id="run-started",
        portfolio_item_id=seeded["item"].id,
        seo_brief_id=seeded["seo"].id,
        input_fingerprint=readiness["input_fingerprint"],
        status="STARTED",
        safe_metadata={},
        started_at=NOW,
    )
    db.add(run)
    db.commit()
    renderer = FakeRenderer()

    with pytest.raises(generation.AutonomousGenerationError, match="GENERATION_ALREADY_STARTED"):
        generation.execute_autonomous_generation(
            db,
            seeded["item"].id,
            settings=_settings(pinterest_autonomous_generation_enabled=True),
            renderer=renderer,
            now=NOW,
        )

    assert renderer.calls == []
    db.close()


def test_input_drift_after_success_blocks_regeneration():
    db = _db()
    seeded = _seed(db)
    settings = _settings(pinterest_autonomous_generation_enabled=True)
    generation.execute_autonomous_generation(
        db,
        seeded["item"].id,
        settings=settings,
        renderer=FakeRenderer(),
        now=NOW,
    )

    seeded["item"] = db.get(PinterestPortfolioPlanItem, seeded["item"].id)
    seeded["item"].item_fingerprint = "7" * 64
    db.commit()

    result = generation.autonomous_generation_readiness(
        db,
        seeded["item"].id,
        settings=settings,
    )
    assert result["ready"] is False
    assert "GENERATION_INPUT_DRIFT" in result["blockers"]
    db.close()


def test_renderer_failure_records_failed_run_and_rolls_back_content_lineage():
    db = _db()
    seeded = _seed(db)
    settings = _settings(pinterest_autonomous_generation_enabled=True)

    with pytest.raises(generation.AutonomousGenerationError, match="AUTONOMOUS_CREATIVE_RENDER_FAILED"):
        generation.execute_autonomous_generation(
            db,
            seeded["item"].id,
            settings=settings,
            renderer=FakeRenderer(fail=True),
            now=NOW,
        )

    run = db.query(PinterestAutonomousGenerationRun).one()
    assert run.status == "FAILED"
    assert run.concept_id is None
    assert run.draft_id is None
    assert run.creative_id is None
    assert db.query(PinConcept).count() == 0
    assert db.query(PinDraft).count() == 0
    assert db.get(PinterestPortfolioPlanItem, seeded["item"].id).status == "PLANNED"
    assert db.query(PinPublication).count() == 0
    db.close()


def test_reserve_slot_is_not_generated_before_promotion():
    db = _db()
    seeded = _seed(db)
    seeded["item"].is_reserve = True
    seeded["item"].planned_date = None
    db.commit()

    result = generation.autonomous_generation_readiness(
        db,
        seeded["item"].id,
        settings=_settings(),
    )
    assert result["ready"] is False
    assert "RESERVE_ITEM_NOT_PROMOTED" in result["blockers"]
    db.close()
