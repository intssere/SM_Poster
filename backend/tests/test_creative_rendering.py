"""Focused, network-free coverage for local creative rendering."""
from io import BytesIO

from PIL import Image, ImageDraw
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models.domain import (
    CreativeTemplate, DraftStatus, PinApproval, PinConcept, PinCreative, PinDraft,
    PinPublication, Product, ProductImage, Store,
)
from app.api.routes import proposals as proposal_routes
from app.services.creative_rendering import (
    DESIGN_TOKEN_VERSION, FRAGRANCE_FOOTER_TEXT, FRAGRANCE_MASTHEAD_TEXT,
    GENERIC_FOOTER_TEXT, GENERIC_MASTHEAD_TEXT, GIFT_SET_PRODUCT_STAGE_BOX,
    PRODUCT_STAGE_BOX, TEXT_PANEL_BOX, CreativeRenderService, CreativeStorage,
    edge_connected_near_white_cutout, render_png,
)
from app.services.fingerprints import creative_fingerprint
from app.services.pin_proposals import PinProposalService


def png(size=(240, 120), color=(30, 80, 120)):
    out = BytesIO()
    Image.new("RGB", size, color).save(out, "PNG")
    return out.getvalue()


def prepared(tmp_path):
    # Reuse the established proposal factory so rationale/evidence stays authentic.
    from test_pin_proposals import add_product
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    store = Store(name="test", shop_domain="test.example")
    db.add(store); db.commit()
    product = add_product(db, store, suffix="creative")
    proposals = PinProposalService(session_factory=factory).generate_controlled_batch(product_limit=1, max_proposals_per_product=1)
    draft = db.get(PinDraft, proposals["representative_proposals"][0]["id"])
    return db, factory, product, draft, CreativeRenderService(factory, downloader=lambda url: png(), storage=CreativeStorage(tmp_path))


def test_exact_canvas_png_determinism_fingerprint_and_all_templates(tmp_path):
    db, factory, product, draft, service = prepared(tmp_path)
    concept = db.get(PinConcept, draft.concept_id)
    image = db.scalar(select(ProductImage).where(ProductImage.product_id == product.id))
    original_checksum = image.source_sha256
    for key in (
        "luxury_product_spotlight", "product_classification",
        "gift_guide_gift_set", "editorial_product_pick",
    ):
        template = db.scalar(select(CreativeTemplate).where(CreativeTemplate.key == key))
        if not template:
            template = CreativeTemplate(key=key, version=1, name=key, definition={})
            db.add(template); db.flush()
        rationale = dict(concept.rationale)
        rationale["creative_template_key"] = key
        rationale["template_version"] = 1
        concept.rationale = rationale
        db.commit()
        result = service.render_review_batch(1)
        assert result["failed"] == 0, result["items"]
        creative = db.scalar(select(PinCreative).where(PinCreative.draft_id == draft.id, PinCreative.template_id == template.id))
        data = service.storage.path_for(creative.id).read_bytes()
        assert Image.open(BytesIO(data)).size == (1000, 1500)
        assert creative.render_spec["template_key"] == key
        assert creative.render_spec["image"]["checksum_sha256"]
        assert isinstance(creative.render_spec["image"]["cutout"]["applied"], bool)
        assert creative.render_spec["image"]["cutout"]["mask_method"] == "edge_connected_near_white_v1"
        assert creative.render_spec["image"]["cutout"]["independent_opaque_background_evidence"] is False
        assert creative.render_spec["image"]["cutout"]["safety_rejection_reason"] == "opaque_background_not_independently_verified"
        assert creative.render_spec["design_token_version"] == DESIGN_TOKEN_VERSION
        assert creative.render_spec["product_category"] == "fragrance"
    # Rendering has never altered catalog provenance/checksum.
    assert image.source_sha256 == original_checksum
    spec = {"a": 1}
    assert creative_fingerprint(source_image_sha256="a" * 64, template_key="x", template_version=1, text_hash="b" * 64, layout_parameters=spec) == creative_fingerprint(source_image_sha256="a" * 64, template_key="x", template_version=1, text_hash="b" * 64, layout_parameters=spec)
    db.close()


def test_polished_geometry_and_design_version():
    assert DESIGN_TOKEN_VERSION == 2
    assert PRODUCT_STAGE_BOX == (150, 180, 850, 840)
    assert GIFT_SET_PRODUCT_STAGE_BOX == (150, 210, 850, 840)
    assert TEXT_PANEL_BOX == (50, 1060, 950, 1450)
    assert (PRODUCT_STAGE_BOX[2] - PRODUCT_STAGE_BOX[0]) < 820
    assert (PRODUCT_STAGE_BOX[3] - PRODUCT_STAGE_BOX[1]) < 790
    assert (TEXT_PANEL_BOX[3] - TEXT_PANEL_BOX[1]) < 450


def test_fragrance_and_generic_branding_are_category_gated(monkeypatch):
    drawn_text: list[str] = []
    original_text = ImageDraw.ImageDraw.text

    def capture_text(self, xy, text, *args, **kwargs):
        drawn_text.append(text)
        return original_text(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture_text)
    source = Image.new("RGBA", (240, 120), (30, 80, 120, 255))
    base = {
        "template_key": "luxury_product_spotlight",
        "headline": "A refined product feature",
        "supporting_text": "Authentic catalog selection",
    }

    render_png({**base, "product_category": "fragrance"}, source)
    assert FRAGRANCE_MASTHEAD_TEXT in drawn_text
    assert FRAGRANCE_FOOTER_TEXT in drawn_text
    assert GENERIC_MASTHEAD_TEXT not in drawn_text
    assert GENERIC_FOOTER_TEXT not in drawn_text

    drawn_text.clear()
    render_png({**base, "product_category": "beauty"}, source)
    assert GENERIC_MASTHEAD_TEXT in drawn_text
    assert GENERIC_FOOTER_TEXT in drawn_text
    assert FRAGRANCE_MASTHEAD_TEXT not in drawn_text
    assert FRAGRANCE_FOOTER_TEXT not in drawn_text

    drawn_text.clear()
    render_png(base, source)
    assert GENERIC_MASTHEAD_TEXT in drawn_text
    assert GENERIC_FOOTER_TEXT in drawn_text
    assert FRAGRANCE_MASTHEAD_TEXT not in drawn_text
    assert FRAGRANCE_FOOTER_TEXT not in drawn_text


def test_design_version_changes_fingerprint_from_v1():
    common = {
        "source_image_sha256": "a" * 64,
        "template_key": "luxury_product_spotlight",
        "template_version": 1,
        "text_hash": "b" * 64,
    }
    v1 = creative_fingerprint(**common, layout_parameters={"design_token_version": 1})
    polished = creative_fingerprint(
        **common,
        layout_parameters={"design_token_version": DESIGN_TOKEN_VERSION},
    )
    assert polished != v1


def test_contain_and_text_overflow_are_deterministic(tmp_path):
    source = Image.open(BytesIO(png((1200, 100))))
    spec = {"template_key": "editorial_product_pick", "headline": "A concise authentic product", "subheadline": "Supporting catalog text"}
    first, second = render_png(spec, source), render_png(spec, source)
    assert first == second
    assert Image.open(BytesIO(first)).size == (1000, 1500)
    # Aspect ratio is preserved: source does not fill the tall image box.
    assert Image.open(BytesIO(first)).getpixel((500, 200)) != Image.open(BytesIO(first)).getpixel((500, 500))
    try:
        render_png({**spec, "headline": "word " * 500}, source)
    except ValueError as exc:
        assert "text" in str(exc).lower() or "overflow" in str(exc).lower()
    else:
        raise AssertionError("Expected controlled text overflow failure")


def test_edge_connected_cutout_removes_only_connected_near_white_pixels():
    source = Image.new("RGBA", (9, 9), (250, 250, 248, 255))
    # This near-white center is enclosed by product pixels and must not be
    # treated as background simply because of its colour.
    for x in range(2, 7):
        for y in range(2, 7):
            source.putpixel((x, y), (40, 80, 120, 255))
    source.putpixel((4, 4), (250, 250, 248, 255))

    cutout, provenance = edge_connected_near_white_cutout(
        source, trusted_opaque_background=True
    )

    assert provenance["applied"] is True
    assert provenance["mask_method"] == "edge_connected_near_white_v1"
    assert provenance["safety_validated"] is True
    assert provenance["independent_opaque_background_evidence"] is True
    assert provenance["alpha_mask_composited"] is True
    assert provenance["retained_pixels_recolored"] is False
    assert cutout.getpixel((0, 0))[3] == 0
    assert cutout.getpixel((4, 4)) == (250, 250, 248, 255)
    assert cutout.getpixel((3, 3)) == (40, 80, 120, 255)


def test_non_cuttable_source_uses_premium_card_fallback_and_layout_is_deterministic():
    source = Image.new("RGBA", (240, 120), (30, 80, 120, 255))
    prepared, provenance = edge_connected_near_white_cutout(
        source, trusted_opaque_background=True
    )
    spec = {
        "template_key": "luxury_product_spotlight",
        "headline": "A restrained editorial product feature",
        "supporting_text": "Authentic catalog selection",
    }

    first = render_png(spec, source, prepared_product=prepared, cutout=provenance)
    second = render_png(spec, source, prepared_product=prepared, cutout=provenance)

    assert provenance["applied"] is False
    assert provenance["fallback_treatment"] == "premium_source_card_v1"
    assert first == second
    assert Image.open(BytesIO(first)).size == (1000, 1500)
    # The card is visibly rendered around the unchanged source image.
    assert Image.open(BytesIO(first)).getpixel((91, 930)) != (30, 80, 120)


def test_edge_touching_light_foreground_fails_closed_to_source_card():
    source = Image.new("RGBA", (40, 40), (252, 252, 251, 255))
    # A light cropped product reaches the left edge.  It is intentionally not
    # eligible for local background removal, even though the rest is white.
    for x in range(0, 22):
        for y in range(9, 31):
            source.putpixel((x, y), (245, 245, 243, 255))

    prepared, provenance = edge_connected_near_white_cutout(
        source, trusted_opaque_background=True
    )

    assert provenance["applied"] is False
    assert provenance["safety_rejection_reason"] == "edge_touching_foreground"
    assert provenance["fallback_treatment"] == "premium_source_card_v1"
    assert prepared.getpixel((0, 20)) == (245, 245, 243, 255)


def test_mostly_near_white_edge_touching_product_with_dark_label_fails_closed():
    source = Image.new("RGBA", (60, 60), (255, 255, 255, 255))
    # The product body itself is near-white and reaches the left edge.  Only
    # its small dark central label is distinguishable from the background.
    # Removing every connected near-white sample would erase the product body.
    for x in range(0, 50):
        for y in range(5, 55):
            source.putpixel((x, y), (249, 249, 249, 255))
    for x in range(22, 37):
        for y in range(22, 37):
            source.putpixel((x, y), (35, 35, 35, 255))

    prepared, provenance = edge_connected_near_white_cutout(
        source, trusted_opaque_background=True
    )

    assert provenance["applied"] is False
    assert provenance["safety_rejection_reason"] == "insufficient_retained_foreground"
    assert provenance["retained_foreground_ratio"] < 0.10
    assert provenance["alpha_mask_composited"] is False
    assert provenance["retained_pixels_recolored"] is False
    assert prepared.tobytes() == source.tobytes()


def test_default_production_cutout_rejects_opaque_40px_light_body_with_dark_label():
    source = Image.new("RGBA", (40, 40), (255, 255, 255, 255))
    # Near-white product body touches the left edge; only a 15x16 dark label
    # distinguishes it from the fully opaque white surroundings.
    for x in range(0, 34):
        for y in range(2, 38):
            source.putpixel((x, y), (249, 249, 249, 255))
    for x in range(12, 27):
        for y in range(12, 28):
            source.putpixel((x, y), (35, 35, 35, 255))

    prepared, provenance = edge_connected_near_white_cutout(source)

    assert provenance["applied"] is False
    assert provenance["independent_opaque_background_evidence"] is False
    assert provenance["safety_rejection_reason"] == "opaque_background_not_independently_verified"
    assert provenance["fallback_treatment"] == "premium_source_card_v1"
    assert provenance["alpha_mask_composited"] is False
    assert prepared.tobytes() == source.tobytes()


def test_provenance_failures_and_idempotence_do_not_mutate_proposal_state(tmp_path):
    db, factory, product, draft, service = prepared(tmp_path)
    before = (draft.status, db.scalar(select(func.count(PinConcept.id))), db.scalar(select(func.count(PinApproval.id))), db.scalar(select(func.count(PinPublication.id))))
    first = service.render_review_batch(1)
    second = service.render_review_batch(1)
    assert first["rendered"] == 1, first["items"]
    assert second["existing"] == 1 and second["failed"] == 0
    assert db.scalar(select(func.count(PinCreative.id))) == 1
    assert (draft.status, db.scalar(select(func.count(PinConcept.id))), db.scalar(select(func.count(PinApproval.id))), db.scalar(select(func.count(PinPublication.id)))) == before
    image = db.scalar(select(ProductImage).where(ProductImage.product_id == product.id))
    image.editorial_eligible = False; db.commit()
    failure = service.render_review_batch(1)
    assert failure["failed"] == 1
    # No publishing integration exists in the renderer; it only writes local files.
    assert failure["unsupported_claims_introduced"] == []
    db.close()

def test_first_render_checksum_becomes_immutable_provenance_baseline(tmp_path):
    db, factory, product, draft, service = prepared(tmp_path)
    assert service.render_review_batch(1)["rendered"] == 1
    creative = db.scalar(select(PinCreative).where(PinCreative.draft_id == draft.id))
    assert creative.render_spec["image"]["checksum_basis"] == "first_verified_render"
    changed = CreativeRenderService(
        factory,
        downloader=lambda _: png(color=(200, 10, 10)),
        storage=CreativeStorage(tmp_path / "changed"),
    )
    result = changed.render_review_batch(1)
    assert result["failed"] == 1
    assert "checksum" in result["items"][0]["error"].lower()
    assert db.scalar(select(func.count(PinCreative.id))) == 1
    assert creative.render_status == "RENDERED"
    db.close()


def test_invalid_source_download_and_decode_failures_are_recorded(tmp_path):
    db, factory, product, draft, service = prepared(tmp_path)
    image = db.scalar(select(ProductImage).where(ProductImage.product_id == product.id))
    image.source_url = "https://example.invalid/x.jpg"; db.commit()
    concept = db.get(PinConcept, draft.concept_id)
    rationale = dict(concept.rationale); rationale["authentic_image"] = {**rationale["authentic_image"], "url": image.source_url}; concept.rationale = rationale; db.commit()
    assert service.render_review_batch(1)["failed"] == 1
    image.source_url = "https://cdn.shopify.com/x.jpg"
    rationale = dict(concept.rationale); rationale["authentic_image"] = {**rationale["authentic_image"], "url": image.source_url}; concept.rationale = rationale; db.commit()
    broken = CreativeRenderService(factory, downloader=lambda _: b"not an image", storage=CreativeStorage(tmp_path / "broken"))
    assert broken.render_review_batch(1)["failed"] == 1
    qa = broken.qa_report()
    assert qa["publishing_enabled"] is False
    assert qa["unsupported_claims_introduced"] == []
    db.close()


def test_persisted_creative_image_route_serves_png_without_rendering(tmp_path, monkeypatch):
    creative_id = "707ec195-bd03-490c-98c9-c7f8436eba44"
    storage = CreativeStorage(tmp_path)
    contents = png()
    storage.write_png(creative_id, contents)
    monkeypatch.setattr(proposal_routes, "CreativeStorage", lambda: storage)

    response = proposal_routes.creative_image(creative_id)

    assert response.path == tmp_path / f"{creative_id}.png"
    assert response.media_type == "image/png"
    assert response.headers["cache-control"] == "private, no-store"
    assert storage.path_for(creative_id).read_bytes() == contents