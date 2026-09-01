from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models.domain import (
    DraftStatus,
    Board,
    Campaign,
    ContentAngle,
    CreativeTemplate,
    KeywordCluster,
    PinApproval,
    PinConcept,
    PinCreative,
    PinDraft,
    PinPublication,
    Product,
    ProductImage,
    ProductIntelligence,
    Store,
)
from app.services.pin_proposals import PinProposalService


PROPOSAL_STATE_MODELS = (
    Board,
    Campaign,
    ContentAngle,
    CreativeTemplate,
    KeywordCluster,
    PinConcept,
    PinDraft,
    PinCreative,
    PinApproval,
    PinPublication,
)


def proposal_state(db):
    state = {}
    for model in PROPOSAL_STATE_MODELS:
        rows = [
            dict(row)
            for row in db.execute(
                select(model.__table__).order_by(*model.__table__.primary_key.columns)
            ).mappings()
        ]
        state[model.__tablename__] = rows
    return state


def setup_service():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    store = Store(name="Diamond Shelf", shop_domain="diamondshelf.test")
    db.add(store)
    db.commit()
    return db, store, PinProposalService(session_factory=factory)


def add_product(
    db,
    store,
    *,
    suffix,
    title="Example Eau de Parfum for Women – 3.4 oz",
    vendor="Gucci",
    category="fragrance",
    audience="women",
    price_band="100_to_249",
    normalization_status="COMPLETE",
    inventory=10,
    tags=None,
):
    product = Product(
        store_id=store.id,
        shopify_product_id=f"gid://shopify/Product/{suffix}",
        handle=f"product-{suffix}",
        title=title,
        vendor=vendor,
        product_type="Perfume & Cologne" if category == "fragrance" else category,
        status="ACTIVE",
        product_url=f"https://diamondshelf.test/products/product-{suffix}",
        tags=tags or [],
        collections=[{"title": "Fragrance"}] if category == "fragrance" else [{"title": category}],
        shopify_data={},
        inventory_total=inventory,
        price_min="149.00" if price_band == "100_to_249" else "39.00",
        excluded_from_editorial=False,
    )
    db.add(product)
    db.flush()
    intelligence = ProductIntelligence(
        product_id=product.id,
        brand=vendor,
        audience=audience,
        designer=vendor if vendor == "Gucci" else None,
        niche=vendor if vendor == "Mancera" else None,
        arabian_classification="arabian" if vendor == "Lattafa" else None,
        fragrance_family="Woody" if category == "fragrance" else None,
        fragrance_notes=[],
        concentration="Eau de Parfum" if category == "fragrance" else None,
        size="3.4 oz",
        price_band=price_band,
        gift_suitability="gift_set" if category == "gift_set" else None,
        image_quality=1,
        image_available=True,
        inventory_eligible=True,
        eligibility_score=95,
        eligibility_status="ELIGIBLE",
        eligibility_reasons=["eligible"],
        normalization_status=normalization_status,
        normalized_data={
            "normalization_category": category,
            "missing_required_fields": [] if normalization_status == "COMPLETE" else ["fragrance_family"],
        },
    )
    image = ProductImage(
        product_id=product.id,
        shopify_media_id=f"gid://shopify/MediaImage/{suffix}",
        source_url=f"https://cdn.shopify.com/product-{suffix}.jpg",
        width=1200,
        height=1500,
        is_primary=True,
        editorial_eligible=True,
    )
    db.add_all([intelligence, image])
    db.commit()
    return product


def add_review_creative(db, draft_id, suffix="review"):
    draft = db.get(PinDraft, draft_id)
    concept = db.get(PinConcept, draft.concept_id)
    template = db.scalar(select(CreativeTemplate).order_by(CreativeTemplate.id))
    image = db.scalar(
        select(ProductImage).where(ProductImage.product_id == concept.product_id)
    )
    creative = PinCreative(
        draft_id=draft.id,
        template_id=template.id,
        source_image_id=image.id,
        creative_fingerprint=f"{suffix:0<64}"[:64],
        render_status="RENDERED",
    )
    db.add(creative)
    db.commit()
    return creative


def test_controlled_proposal_uses_authentic_image_and_fact_safe_copy():
    db, store, service = setup_service()
    product = add_product(db, store, suffix="1")

    report = service.generate_controlled_batch(product_limit=1, max_proposals_per_product=2)

    assert report["products_selected"] == 1
    assert report["proposals_generated"] == 2
    assert report["unsupported_claims_detected"] == []
    proposal = report["representative_proposals"][0]
    assert proposal["product_id"] == product.id
    assert proposal["image_url"] == "https://cdn.shopify.com/product-1.jpg"
    assert proposal["canonical_url"] == product.product_url
    assert "utm_source=pinterest" in proposal["utm_url"]
    assert proposal["approval_status"] == "REVIEW"
    assert proposal["intended_board"]["pinterest_board_id"] is None
    assert len(proposal["duplicate_fingerprint"]) == 64
    assert "longevity" not in proposal["description"].lower()
    assert "discount" not in proposal["description"].lower()
    db.close()


def test_luxury_angle_requires_explicit_catalog_evidence():
    db, store, service = setup_service()
    product = add_product(db, store, suffix="1", price_band="100_to_249")

    without_label = service.generate_controlled_batch(product_limit=1, max_proposals_per_product=2)

    assert "Luxury Product Spotlight" not in without_label["content_angle_distribution"]
    db.query(PinDraft).delete()
    db.query(PinConcept).delete()
    product.tags = ["Luxury"]
    db.commit()
    with_label = service.generate_controlled_batch(product_limit=1, max_proposals_per_product=2)
    assert "Luxury Product Spotlight" in with_label["content_angle_distribution"]
    db.close()


def test_non_editorial_or_non_shopify_images_are_rejected():
    db, store, service = setup_service()
    product = add_product(db, store, suffix="1")
    image = db.scalar(select(ProductImage).where(ProductImage.product_id == product.id))
    image.editorial_eligible = False
    db.commit()

    report = service.generate_controlled_batch(product_limit=1)

    assert report["products_selected"] == 0
    assert report["proposals_generated"] == 0
    image.editorial_eligible = True
    image.shopify_media_id = None
    db.commit()
    report = service.generate_controlled_batch(product_limit=1)
    assert report["products_selected"] == 0
    assert report["proposals_generated"] == 0
    db.close()


def test_exact_duplicate_fingerprints_are_prevented():
    db, store, service = setup_service()
    add_product(db, store, suffix="1")

    first = service.generate_controlled_batch(product_limit=1, max_proposals_per_product=2)
    second = service.generate_controlled_batch(product_limit=1, max_proposals_per_product=2)

    assert first["proposals_generated"] == 2
    assert second["proposals_generated"] == 0
    assert second["duplicate_attempts_prevented"] == 2
    assert db.scalar(select(func.count(PinConcept.id))) == 2
    assert db.scalar(select(func.count(PinDraft.id))) == 2
    db.close()


def test_evidence_gates_angles_and_filters_selection():
    db, store, service = setup_service()
    add_product(
        db,
        store,
        suffix="1",
        title="Candle – 10 oz",
        vendor="Candle House",
        category="home_fragrance",
        audience=None,
        price_band="under_50",
    )
    add_product(
        db,
        store,
        suffix="2",
        vendor="Lattafa",
        audience="men",
        price_band="50_to_99",
    )

    report = service.generate_controlled_batch(
        product_limit=1,
        max_proposals_per_product=2,
        filters={"category": "home_fragrance"},
    )

    assert report["products_selected"] == 1
    assert set(report["content_angle_distribution"]).issubset({
        "Home Fragrance",
        "Price-Focused Product Pick",
    })
    assert "Men's Fragrance" not in report["content_angle_distribution"]
    assert "Arabian Fragrance" not in report["content_angle_distribution"]
    db.close()


def test_approval_requires_review_and_creates_audit_decision():
    db, store, service = setup_service()
    add_product(db, store, suffix="1")
    report = service.generate_controlled_batch(product_limit=1, max_proposals_per_product=1)
    draft_id = report["representative_proposals"][0]["id"]
    creative = add_review_creative(db, draft_id)

    result = service.decide(
        draft_id,
        "APPROVED",
        "Fact-safe and ready.",
        reviewed_creative_id=creative.id,
    )

    assert result == {
        "id": draft_id,
        "approval_status": "APPROVED",
        "publishing_enabled": False,
    }
    assert db.get(PinDraft, draft_id).status == DraftStatus.APPROVED
    approval = db.scalar(select(PinApproval).where(PinApproval.draft_id == draft_id))
    assert approval.decision == "APPROVED"
    assert approval.decided_by == "manual_dashboard_action"
    assert approval.approved_version_id == "original"
    assert approval.revision_id is None
    assert approval.creative_id == creative.id
    try:
        service.decide(draft_id, "REJECTED")
    except ValueError as exc:
        assert "Only proposals in REVIEW" in str(exc)
    else:
        raise AssertionError("Expected a second decision to be rejected")
    db.close()


def test_batch_is_capped_at_twenty_products_and_forty_proposals():
    db, store, service = setup_service()
    for index in range(25):
        add_product(
            db,
            store,
            suffix=str(index),
            vendor=["Gucci", "Mancera", "Lattafa"][index % 3],
            audience=["men", "women", "unisex"][index % 3],
            price_band=["under_50", "50_to_99", "100_to_249"][index % 3],
        )

    report = service.generate_controlled_batch(product_limit=20, max_proposals_per_product=2)

    assert report["products_selected"] == 20
    assert report["proposals_generated"] <= 40
    assert db.scalar(select(func.count(PinConcept.id))) <= 40
    db.close()


def test_new_arrival_does_not_starve_supported_classification_angles():
    db, store, service = setup_service()
    for index, (vendor, audience) in enumerate((
        ("Gucci", "women"),
        ("Lattafa", "unisex"),
        ("Mancera", "men"),
    )):
        add_product(
            db,
            store,
            suffix=str(index),
            vendor=vendor,
            audience=audience,
            tags=["New Arrival"],
        )

    report = service.generate_controlled_batch(
        product_limit=3,
        max_proposals_per_product=2,
        dry_run=True,
    )

    assert report["content_angle_distribution"]["Designer Fragrance"] == 1
    assert report["content_angle_distribution"]["Arabian Fragrance"] == 1
    assert report["content_angle_distribution"]["Niche Fragrance"] == 1
    assert report["content_angle_distribution"].get("New Arrival", 0) < 3
    db.close()


def test_unsupported_classification_angles_remain_impossible():
    db, store, service = setup_service()
    add_product(
        db,
        store,
        suffix="1",
        vendor="Unknown House",
        audience="unisex",
        tags=["New Arrival"],
    )

    report = service.generate_controlled_batch(product_limit=1, dry_run=True)
    available_keys = {
        candidate["angle_key"]
        for product in report["candidate_angle_diagnostics"]
        for candidate in product["candidate_angles"]
    }

    assert not {"designer-fragrance", "arabian-fragrance", "niche-fragrance"} & available_keys
    assert all(values["available_candidates"] == 0 for values in report["classification_angle_coverage"].values())
    db.close()


def test_two_ranked_proposals_have_distinct_editorial_intents():
    db, store, service = setup_service()
    add_product(
        db,
        store,
        suffix="1",
        vendor="Lattafa",
        audience="unisex",
        tags=["New Arrival"],
    )

    report = service.generate_controlled_batch(product_limit=1, dry_run=True)
    selected = report["selected_angle_details"]

    assert len(selected) == 2
    assert len({candidate["intent_group"] for candidate in selected}) == 2
    assert {candidate["angle"] for candidate in selected} == {
        "Arabian Fragrance",
        "Unisex Fragrance",
    }
    db.close()


def test_dry_run_is_fully_non_persisting_and_reports_selection_reasons():
    db, store, service = setup_service()
    add_product(
        db,
        store,
        suffix="1",
        vendor="Gucci",
        audience="women",
        tags=["New Arrival"],
    )
    before = {
        "concepts": db.scalar(select(func.count(PinConcept.id))),
        "drafts": db.scalar(select(func.count(PinDraft.id))),
        "approvals": db.scalar(select(func.count(PinApproval.id))),
    }

    report = service.generate_controlled_batch(product_limit=1, dry_run=True)

    after = {
        "concepts": db.scalar(select(func.count(PinConcept.id))),
        "drafts": db.scalar(select(func.count(PinDraft.id))),
        "approvals": db.scalar(select(func.count(PinApproval.id))),
    }
    assert after == before
    assert report["dry_run"] is True
    assert report["mutations_performed"] == 0
    assert report["selected_angle_details"]
    assert all(candidate["reason"] for candidate in report["selected_angle_details"])
    assert report["rejected_candidate_angles"]
    db.close()


def test_historical_batch_dry_run_preserves_every_proposal_record():
    db, store, service = setup_service()
    products = [
        add_product(db, store, suffix="1", vendor="Gucci", audience="women"),
        add_product(db, store, suffix="2", vendor="Lattafa", audience="unisex"),
    ]
    seeded = service.generate_controlled_batch(
        product_limit=2,
        max_proposals_per_product=2,
    )
    assert seeded["proposals_generated"] == 4

    rejected_draft_id = seeded["representative_proposals"][0]["id"]
    service.decide(rejected_draft_id, "REJECTED", "Keep this proposal out of review.")
    rejected_draft = db.get(PinDraft, rejected_draft_id)
    rejected_concept = db.get(PinConcept, rejected_draft.concept_id)
    creative_template = db.scalar(
        select(CreativeTemplate).where(
            CreativeTemplate.key == "product_classification",
        )
    )
    product_image = db.scalar(
        select(ProductImage).where(ProductImage.product_id == rejected_concept.product_id)
    )
    db.add(PinCreative(
        draft_id=rejected_draft.id,
        template_id=creative_template.id,
        source_image_id=product_image.id,
        rendered_url="https://cdn.diamondshelf.test/test-render.png",
        sha256="a" * 64,
        creative_fingerprint="b" * 64,
        width=1000,
        height=1500,
    ))
    db.commit()

    before = proposal_state(db)
    before_counts = {table: len(rows) for table, rows in before.items()}
    before_review_ids = {
        draft.id
        for draft in db.scalars(
            select(PinDraft).where(PinDraft.status == DraftStatus.READY_FOR_REVIEW)
        )
    }
    before_approval_ids = {approval.id for approval in db.scalars(select(PinApproval))}

    report = service.generate_controlled_batch(
        product_limit=20,
        max_proposals_per_product=2,
        dry_run=True,
    )

    db.expire_all()
    after = proposal_state(db)
    after_counts = {table: len(rows) for table, rows in after.items()}
    after_review_ids = {
        draft.id
        for draft in db.scalars(
            select(PinDraft).where(PinDraft.status == DraftStatus.READY_FOR_REVIEW)
        )
    }
    after_approval_ids = {approval.id for approval in db.scalars(select(PinApproval))}

    assert after == before
    assert after_counts == before_counts
    assert before_review_ids == after_review_ids
    assert before_approval_ids == after_approval_ids
    assert not db.new
    assert not db.dirty
    assert not db.deleted

    assert report["dry_run"] is True
    assert report["sample_source"] == "historical_review_batch"
    assert report["mutations_performed"] == 0
    assert report["products_selected"] == len(products)
    assert report["proposals_generated"] == 4
    assert report["candidate_angle_diagnostics"]
    assert report["selected_angle_details"]
    assert report["rejected_candidate_angles"]
    assert report["content_angle_distribution"]
    assert report["maximum_angle_share"]["share"] > 0
    assert report["classification_angle_coverage"]
    assert db.scalar(select(func.count(PinConcept.id))) == before_counts["pin_concepts"]
    assert db.scalar(select(func.count(PinDraft.id))) == before_counts["pin_drafts"]
    assert db.scalar(select(func.count(PinApproval.id))) == before_counts["pin_approvals"]
    assert db.scalar(select(func.count(PinPublication.id))) == before_counts["pin_publications"] == 0
    db.close()
