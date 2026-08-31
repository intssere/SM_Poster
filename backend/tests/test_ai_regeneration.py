"""Safety and lineage coverage for the AI-assisted regeneration foundation."""
from sqlalchemy import func, select

from app.models.domain import (
    AISettings,
    ContentRevision,
    ContentVersionSelection,
    DraftStatus,
    PinApproval,
    PinCreative,
    PinDraft,
    PinPublication,
    ProductImage,
)
from app.services.ai_regeneration import (
    AIRegenerationError,
    AIRegenerationService,
    AISettingsService,
)
from app.services.creative_rendering import CreativeRenderService, CreativeStorage
from app.services.pin_proposals import PinProposalService

from test_creative_rendering import png
from test_pin_proposals import add_product, setup_service


def test_ai_settings_are_disabled_by_default_and_background_generation_requires_opt_in(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    db, store, proposal_service = setup_service()
    settings = AISettingsService(proposal_service.session_factory)

    default = settings.get()

    assert default["enabled"] is False
    assert default["effective_mode"] == "disabled"
    assert default["credentials_configured"] is False
    assert default["capabilities"]["decorative_backgrounds"] is True
    assert default["decorative_backgrounds_enabled"] is False
    assert default["hosted_model"] == "gpt-5.6-luna"
    assert default["image_model"] == "gpt-image-2"
    assert default["hosted_model_options"] == [
        {"id": "gpt-5.6-luna", "pricing_configured": True, "automatic_selection": True},
        {"id": "gpt-5.6-terra", "pricing_configured": False, "automatic_selection": False},
    ]
    local = settings.update(
        enabled=True,
        provider_mode="local_free",
        decorative_backgrounds_enabled=False,
    )
    assert local["effective_mode"] == "local_free"
    assert local["credentials_configured"] is False
    hosted = settings.update(
        enabled=True,
        provider_mode="hosted_paid",
        decorative_backgrounds_enabled=True,
        image_model="gpt-image-2",
        video_model="gpt-4o-mini",
        per_request_cost_usd=0.25,
    )
    assert hosted["decorative_backgrounds_enabled"] is True
    assert hosted["image_model"] == "gpt-image-2"
    assert hosted["per_request_cost_usd"] == 0.25
    assert db.scalar(select(func.count(AISettings.id))) == 1
    db.close()


def test_ai_settings_reject_unapproved_hosted_models():
    db, store, proposal_service = setup_service()
    settings = AISettingsService(proposal_service.session_factory)
    for field, value in (
        ("hosted_model", "arbitrary-compatible-model"),
        ("image_model", "arbitrary-image-model"),
    ):
        try:
            settings.update(**{field: value})
        except AIRegenerationError as exc:
            assert "Unsupported hosted" in str(exc)
        else:
            raise AssertionError(f"Unapproved {field} must be rejected")
    db.close()


def test_copy_regeneration_is_immutable_fact_safe_and_does_not_activate_itself():
    db, store, proposal_service = setup_service()
    product = add_product(db, store, suffix="copy")
    report = proposal_service.generate_controlled_batch(product_limit=1, max_proposals_per_product=1)
    draft_id = report["representative_proposals"][0]["id"]
    original = db.get(PinDraft, draft_id)
    original_state = (
        original.title,
        original.description,
        original.alt_text,
        original.text_fingerprint,
        original.status,
    )
    before = {
        "approvals": db.scalar(select(func.count(PinApproval.id))),
        "publications": db.scalar(select(func.count(PinPublication.id))),
        "creatives": db.scalar(select(func.count(PinCreative.id))),
    }

    revision = AIRegenerationService(proposal_service.session_factory).regenerate_copy(draft_id)

    db.expire_all()
    assert (
        original.title,
        original.description,
        original.alt_text,
        original.text_fingerprint,
        original.status,
    ) == original_state
    assert revision["version"] == 2
    assert revision["kind"] == "COPY"
    assert revision["status"] == "REVIEW"
    assert revision["active"] is False
    assert revision["generation_mode"] == "deterministic_fallback"
    assert revision["unsupported_claims"] == []
    assert revision["facts_used"]["title"] == product.title
    assert revision["creative"] is None
    assert db.scalar(select(func.count(ContentVersionSelection.id))) == 0
    assert {
        "approvals": db.scalar(select(func.count(PinApproval.id))),
        "publications": db.scalar(select(func.count(PinPublication.id))),
        "creatives": db.scalar(select(func.count(PinCreative.id))),
    } == before
    db.close()


def test_revision_lineage_and_active_selection_are_explicit_and_reversible():
    db, store, proposal_service = setup_service()
    add_product(db, store, suffix="lineage")
    draft_id = proposal_service.generate_controlled_batch(
        product_limit=1, max_proposals_per_product=1
    )["representative_proposals"][0]["id"]
    service = AIRegenerationService(proposal_service.session_factory)

    first = service.regenerate_copy(draft_id)
    before_selection = service.versions(draft_id)
    assert before_selection["active_version_number"] == 1
    assert before_selection["versions"][0]["active"] is True

    selected = service.select_version(draft_id, first["id"])
    assert selected["active_version_number"] == 2
    second = service.regenerate_copy(draft_id)
    assert second["parent_revision_id"] == first["id"]
    assert service.versions(draft_id)["active_version_number"] == 2

    service.select_version(draft_id, second["id"])
    assert service.versions(draft_id)["active_version_number"] == 3
    restored = service.select_version(draft_id, "original")
    assert restored["active_version_number"] == 1
    assert restored["versions"][0]["active"] is True
    assert db.get(PinDraft, draft_id).status == DraftStatus.READY_FOR_REVIEW
    assert db.scalar(select(func.count(PinApproval.id))) == 0
    assert db.scalar(select(func.count(PinPublication.id))) == 0
    db.close()


def test_hosted_mode_without_credentials_falls_back_without_original_proposal_mutation(monkeypatch):
    db, store, proposal_service = setup_service()
    add_product(db, store, suffix="hosted")
    draft_id = proposal_service.generate_controlled_batch(
        product_limit=1, max_proposals_per_product=1
    )["representative_proposals"][0]["id"]
    AISettingsService(proposal_service.session_factory).update(
        enabled=True,
        provider_mode="hosted_paid",
        decorative_backgrounds_enabled=False,
    )
    draft = db.get(PinDraft, draft_id)
    before = (draft.title, draft.status, db.scalar(select(func.count(ContentRevision.id))))

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    revision = AIRegenerationService(proposal_service.session_factory).regenerate_copy(draft_id)

    db.expire_all()
    assert (draft.title, draft.status) == before[:2]
    assert db.scalar(select(func.count(ContentRevision.id))) == before[2] + 1
    assert revision["generation_mode"] == "fallback_missing_credentials"
    assert revision["provider_mode"] == "hosted_paid"
    assert db.scalar(select(func.count(PinApproval.id))) == 0
    assert db.scalar(select(func.count(PinPublication.id))) == 0
    db.close()


def test_creative_variant_preserves_original_render_and_shopify_provenance(tmp_path):
    db, store, proposal_service = setup_service()
    product = add_product(db, store, suffix="variant")
    draft_id = proposal_service.generate_controlled_batch(
        product_limit=1, max_proposals_per_product=1
    )["representative_proposals"][0]["id"]
    renderer = CreativeRenderService(
        proposal_service.session_factory,
        downloader=lambda _: png(),
        storage=CreativeStorage(tmp_path),
    )
    assert renderer.render_review_batch(1)["rendered"] == 1
    original = db.scalar(
        select(PinCreative).where(PinCreative.draft_id == draft_id).order_by(PinCreative.created_at)
    )
    original_state = (
        original.id,
        original.sha256,
        original.creative_fingerprint,
        original.render_spec,
    )
    original_template = original.render_spec["template_key"]
    other_template = next(key for key in (
        "luxury_product_spotlight",
        "product_classification",
        "gift_guide_gift_set",
        "editorial_product_pick",
    ) if key != original_template)
    service = AIRegenerationService(
        proposal_service.session_factory,
        creative_renderer=renderer,
    )

    revision = service.regenerate_creative(draft_id, other_template)

    db.expire_all()
    image = db.scalar(select(ProductImage).where(ProductImage.product_id == product.id))
    assert db.scalar(select(func.count(PinCreative.id))) == 2
    assert (
        original.id,
        original.sha256,
        original.creative_fingerprint,
        original.render_spec,
    ) == original_state
    assert revision["kind"] == "CREATIVE"
    assert revision["creative"]["id"] != original.id
    assert revision["provenance"]["shopify_media_id"] == image.shopify_media_id
    assert revision["provenance"]["provenance_url"] == image.source_url
    assert revision["provenance"]["generated_background"] is False
    assert db.scalar(select(func.count(ContentVersionSelection.id))) == 0
    assert db.get(PinDraft, draft_id).status == DraftStatus.READY_FOR_REVIEW
    assert db.scalar(select(func.count(PinApproval.id))) == 0
    assert db.scalar(select(func.count(PinPublication.id))) == 0
    db.close()


def test_creative_and_revision_are_rolled_back_together_when_revision_commit_fails(tmp_path, monkeypatch):
    db, store, proposal_service = setup_service()
    add_product(db, store, suffix="atomic")
    draft_id = proposal_service.generate_controlled_batch(
        product_limit=1, max_proposals_per_product=1
    )["representative_proposals"][0]["id"]
    renderer = CreativeRenderService(
        proposal_service.session_factory,
        downloader=lambda _: png(),
        storage=CreativeStorage(tmp_path),
    )
    assert renderer.render_review_batch(1)["rendered"] == 1
    before = db.scalar(select(func.count(PinCreative.id)))
    original_add = db.add

    def reject_revision(instance):
        if isinstance(instance, ContentRevision):
            raise RuntimeError("simulated revision persistence failure")
        return original_add(instance)

    monkeypatch.setattr(db, "add", reject_revision)
    service = AIRegenerationService(
        proposal_service.session_factory,
        creative_renderer=renderer,
    )
    service.session_factory = lambda: db
    original_template = db.scalar(
        select(PinCreative).where(PinCreative.draft_id == draft_id)
    ).render_spec["template_key"]
    other_template = next(key for key in (
        "luxury_product_spotlight",
        "product_classification",
        "gift_guide_gift_set",
        "editorial_product_pick",
    ) if key != original_template)

    try:
        service.regenerate_creative(draft_id, other_template)
    except RuntimeError as exc:
        assert "simulated" in str(exc)
    else:
        raise AssertionError("The simulated persistence failure must propagate")

    assert db.scalar(select(func.count(PinCreative.id))) == before
    assert db.scalar(select(func.count(ContentRevision.id))) == 0
    db.close()


def test_selected_copy_keeps_original_rendered_creative_in_proposal_gallery_payload(tmp_path):
    db, store, proposal_service = setup_service()
    add_product(db, store, suffix="copy-gallery")
    draft_id = proposal_service.generate_controlled_batch(
        product_limit=1, max_proposals_per_product=1
    )["representative_proposals"][0]["id"]
    renderer = CreativeRenderService(
        proposal_service.session_factory,
        downloader=lambda _: png(),
        storage=CreativeStorage(tmp_path),
    )
    assert renderer.render_review_batch(1)["rendered"] == 1
    service = AIRegenerationService(
        proposal_service.session_factory,
        creative_renderer=renderer,
    )
    copy_revision = service.regenerate_copy(draft_id)
    service.select_version(draft_id, copy_revision["id"])

    proposal_payload = next(
        item for item in proposal_service.list_proposals(status="REVIEW", limit=100)
        if item["id"] == draft_id
    )

    assert proposal_payload["active_version"] == 2
    assert proposal_payload["creative"]["status"] == "RENDERED"
    assert proposal_payload["creative"]["image_url"]
    assert draft_id in {
        item["id"]
        for item in proposal_service.list_proposals(status="REVIEW", limit=100)
        if item["creative"] and item["creative"]["status"] == "RENDERED"
    }
    db.close()