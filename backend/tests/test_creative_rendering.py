"""Focused, network-free coverage for local creative rendering."""
from io import BytesIO

from PIL import Image
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models.domain import (
    CreativeTemplate, DraftStatus, PinApproval, PinConcept, PinCreative, PinDraft,
    PinPublication, Product, ProductImage, Store,
)
from app.services.creative_rendering import CreativeRenderService, CreativeStorage, render_png
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
        assert creative.render_spec["design_token_version"] == 1
    # Rendering has never altered catalog provenance/checksum.
    assert image.source_sha256 == original_checksum
    spec = {"a": 1}
    assert creative_fingerprint(source_image_sha256="a" * 64, template_key="x", template_version=1, text_hash="b" * 64, layout_parameters=spec) == creative_fingerprint(source_image_sha256="a" * 64, template_key="x", template_version=1, text_hash="b" * 64, layout_parameters=spec)
    db.close()


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