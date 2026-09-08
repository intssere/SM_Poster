import json
from decimal import Decimal

from sqlalchemy import func, select

from app.models.domain import (
    AIGeneratedAsset,
    AIRequestTelemetry,
    AISettings,
    ContentRevision,
    ContentVersionSelection,
    DraftStatus,
    PinApproval,
    PinCreative,
    PinDraft,
    PinPublication,
)
from app.api.routes import proposals as proposal_routes
from app.schemas.pins import RegenerationRequest
from app.services.ai_creative_generation import (
    AICreativeGenerationError,
    AICreativeGenerationService,
    AIGeneratedAssetStorage,
)
from app.services.ai_providers import ImageGenerationResult, TextGenerationResult
from app.services.ai_regeneration import AISettingsService, _cost, _pricing
from app.services.creative_rendering import CreativeRenderService, CreativeStorage

from test_creative_rendering import png
from test_pin_proposals import add_product, setup_service


class FakeImageProvider:
    name = "openai"
    model = "gpt-image-2"

    def __init__(self, image_bytes=None):
        self.image_bytes = image_bytes if image_bytes is not None else png((1024, 1536), (80, 60, 40))
        self.calls = 0

    def generate_background(self, prompt):
        self.calls += 1
        assert "product" not in prompt.lower()
        return ImageGenerationResult(self.image_bytes, self.model)

    def validate_background(self, image_bytes):
        assert image_bytes.startswith(b"\x89PNG")
        return {
            "background_only": True,
            "contains_product": False,
            "contains_packaging": False,
            "contains_logo": False,
            "contains_text": False,
            "contains_person": False,
        }


class FakeTextProvider:
    name = "ollama"
    model = "safe-local"

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0
        self.schemas = []

    def generate(self, prompt, *, schema=None, schema_name="social_copy"):
        self.calls += 1
        self.schemas.append((schema_name, schema))
        assert "authentic Shopify source image" in prompt
        return TextGenerationResult(json.dumps(self.payload), self.model, 40, 80, 120)


def prepared(tmp_path, suffix):
    db, store, proposal_service = setup_service()
    product = add_product(db, store, suffix=suffix)
    draft_id = proposal_service.generate_controlled_batch(
        product_limit=1, max_proposals_per_product=1
    )["representative_proposals"][0]["id"]
    renderer = CreativeRenderService(
        proposal_service.session_factory,
        downloader=lambda _: png(),
        storage=CreativeStorage(tmp_path / "creatives"),
    )
    service = AICreativeGenerationService(
        proposal_service.session_factory,
        renderer=renderer,
        asset_storage=AIGeneratedAssetStorage(tmp_path / "assets"),
    )
    return db, product, proposal_service, draft_id, service


def configure_test_image_price(db):
    settings = db.scalar(select(AISettings))
    settings.pricing_metadata = {
        **(settings.pricing_metadata or {}),
        "gpt-image-2": {"per_image": 0.04},
    }
    db.commit()


def test_background_generation_composites_authentic_source_and_preserves_legacy_rows(tmp_path):
    db, product, proposal_service, draft_id, service = prepared(tmp_path, "ai-background")
    provider = FakeImageProvider()
    service.image_provider_factory = lambda _: provider
    AISettingsService(proposal_service.session_factory).update(
        enabled=True, provider_mode="hosted_paid", decorative_backgrounds_enabled=True,
        per_request_cost_usd=0.25,
    )
    configure_test_image_price(db)
    original = db.get(PinDraft, draft_id)
    original_state = (original.title, original.description, original.text_fingerprint)

    revision = service.generate_background(draft_id, "quiet_luxury", "instagram")

    db.expire_all()
    asset = db.get(AIGeneratedAsset, revision["background_asset_id"])
    creative = db.get(PinCreative, revision["creative"]["id"])
    telemetry = db.scalar(select(AIRequestTelemetry))
    assert provider.calls == 1
    assert revision["kind"] == "IMAGE_BACKGROUND"
    assert revision["generation_type"] == "image_background"
    assert revision["intended_channel"] == "instagram"
    assert revision["active"] is False
    assert asset.mime_type == "image/png"
    assert asset.width == 1024 and asset.height == 1536
    assert asset.provenance["background_only"] is True
    assert asset.provenance["product_image_generated"] is False
    assert creative.render_spec["background"]["asset_id"] == asset.id
    assert creative.render_spec["image"]["shopify_media_id"]
    assert creative.render_spec["image"]["cutout"]["mask_method"] == "edge_connected_near_white_v1"
    assert creative.render_spec["image"]["cutout"]["independent_opaque_background_evidence"] is False
    assert creative.render_spec["image"]["source_bytes_unchanged"] is True
    assert creative.render_spec["image"]["alpha_mask_composited"] is False
    assert db.get(ContentRevision, revision["id"]).provenance["product_cutout"]["applied"] is False
    assert db.get(ContentRevision, revision["id"]).provenance["authentic_product_source_checksum_preserved"] is True
    assert db.get(ContentRevision, revision["id"]).provenance["authentic_product_image_composited_unchanged"] is True
    assert revision["estimated_cost_usd"] is not None
    assert revision["actual_cost_usd"] is None
    assert telemetry.estimated_cost_usd > Decimal("0")
    assert telemetry.actual_cost_usd is None
    assert db.get(ContentRevision, revision["id"]).background_asset.id == asset.id
    assert (original.title, original.description, original.text_fingerprint) == original_state
    assert db.scalar(select(func.count(PinPublication.id))) == 0
    db.close()


def test_image_background_revision_rejection_is_scoped_and_restores_original(tmp_path, monkeypatch):
    db, product, proposal_service, draft_id, service = prepared(tmp_path, "revision-reject")
    provider = FakeImageProvider()
    service.image_provider_factory = lambda _: provider
    AISettingsService(proposal_service.session_factory).update(
        enabled=True, provider_mode="hosted_paid", decorative_backgrounds_enabled=True,
        per_request_cost_usd=0.25,
    )
    configure_test_image_price(db)
    revision = service.generate_background(draft_id, "quiet_luxury")
    service.regeneration.select_version(draft_id, revision["id"])
    assert db.scalar(select(ContentVersionSelection)).revision_id == revision["id"]

    # Exercise the POST handler with the real revision-scoped service.
    monkeypatch.setattr(proposal_routes, "AIRegenerationService", lambda: service.regeneration)
    payload = proposal_routes.reject_image_background_revision(draft_id, revision["id"])
    db.expire_all()

    assert payload["status"] == "REJECTED"
    assert payload["active"] is False
    assert db.get(ContentRevision, revision["id"]).status == "REJECTED"
    assert db.scalar(select(ContentVersionSelection)) is None
    assert db.get(PinDraft, draft_id).status == DraftStatus.READY_FOR_REVIEW
    assert db.scalar(select(func.count(PinApproval.id))) == 0
    assert db.scalar(select(func.count(PinPublication.id))) == 0

    # A proposal may already be rejected; closing its selected background must
    # not reopen or otherwise change the proposal state.
    another = service.generate_background(draft_id, "modern_gradient")
    service.regeneration.select_version(draft_id, another["id"])
    draft = db.get(PinDraft, draft_id)
    draft.status = DraftStatus.REJECTED
    db.commit()
    service.regeneration.reject_image_background_revision(draft_id, another["id"])
    db.expire_all()
    assert db.get(ContentRevision, another["id"]).status == "REJECTED"
    assert db.get(PinDraft, draft_id).status == DraftStatus.REJECTED
    assert db.scalar(select(ContentVersionSelection)) is None
    assert db.scalar(select(func.count(PinApproval.id))) == 0
    assert db.scalar(select(func.count(PinPublication.id))) == 0
    db.close()


def test_image_background_regeneration_rejects_count_other_than_one(monkeypatch):
    class GenerationMustNotRun:
        def generate_background(self, *args, **kwargs):
            raise AssertionError("paid image generation must not be invoked")

    monkeypatch.setattr(proposal_routes, "AICreativeGenerationService", GenerationMustNotRun)
    request = RegenerationRequest(kind="image_background", count=2, style_key="quiet_luxury")
    try:
        proposal_routes.regenerate_proposal("draft-id", request)
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 409
        assert "one at a time" in str(exc.detail)
    else:
        raise AssertionError("Image-background count > 1 should be blocked")


def test_background_validation_and_per_request_ceiling_fail_without_variants(tmp_path):
    db, product, proposal_service, draft_id, service = prepared(tmp_path, "ai-block")
    settings = AISettingsService(proposal_service.session_factory)
    settings.update(
        enabled=True, provider_mode="hosted_paid", decorative_backgrounds_enabled=True,
        per_request_cost_usd=0.01,
    )
    configure_test_image_price(db)
    provider = FakeImageProvider()
    service.image_provider_factory = lambda _: provider

    try:
        service.generate_background(draft_id, "modern_gradient")
    except AICreativeGenerationError as exc:
        assert "per-request" in str(exc)
    else:
        raise AssertionError("Per-request ceiling should block paid generation")
    assert provider.calls == 0
    assert db.scalar(select(func.count(AIGeneratedAsset.id))) == 0
    assert db.scalar(select(func.count(ContentRevision.id))) == 0

    settings.update(per_request_cost_usd=0.25)
    service.image_provider_factory = lambda _: FakeImageProvider(b"not-an-image")
    try:
        service.generate_background(draft_id, "modern_gradient")
    except AICreativeGenerationError:
        pass
    else:
        raise AssertionError("Invalid provider image should be rejected")
    assert db.scalar(select(func.count(AIGeneratedAsset.id))) == 0
    assert db.scalar(select(func.count(ContentRevision.id))) == 0
    assert db.scalar(select(func.count(AIRequestTelemetry.id))) == 2
    assert all(
        row.actual_cost_usd is None
        for row in db.scalars(select(AIRequestTelemetry)).all()
    )
    db.close()


def test_gpt_image_2_medium_portrait_has_safe_default_price_and_unknown_shapes_fail_closed(tmp_path):
    db, product, proposal_service, draft_id, service = prepared(tmp_path, "image-2-priced")
    AISettingsService(proposal_service.session_factory).update()
    settings = db.scalar(select(AISettings))
    classifier_cost = _cost(1000, 120, _pricing(settings, settings.hosted_model))

    estimate = service._image_cost(settings)
    assert estimate == Decimal("0.041") + classifier_cost
    service._paid_preflight(db, settings, estimate)
    assert service._image_cost(settings, size="1024x1024") is None
    assert service._image_cost(settings, quality="high") is None
    try:
        service._paid_preflight(db, settings, service._image_cost(settings, quality="high"))
    except AICreativeGenerationError as exc:
        assert "pricing is unknown" in str(exc)
    else:
        raise AssertionError("Unsupported image quality must fail before a provider call")

    settings.image_model = "unknown-image-model"
    assert service._image_cost(settings) is None
    db.close()


def test_video_script_is_reviewable_spec_not_production_video(tmp_path):
    db, product, proposal_service, draft_id, service = prepared(tmp_path, "video-spec")
    payload = {
        "concept": f"Editorial catalog view for {product.title}",
        "hook": f"Discover {product.title}",
        "script": f"Explore {product.title} with the authentic catalog image.",
        "caption": f"Discover {product.title}",
        "overlay_text": [product.title, "Discover"],
        "cta": "Discover",
        "scenes": [{
            "index": 1, "duration_seconds": 3,
            "visual": "Authentic Shopify product image in center frame.",
            "voiceover": f"Discover {product.title}", "overlay": product.title,
        }],
    }
    provider = FakeTextProvider(payload)
    provider.name = "openai"
    provider.model = "gpt-4o-mini"
    service.video_provider_factory = lambda _: provider
    AISettingsService(proposal_service.session_factory).update(
        enabled=True, provider_mode="hosted_paid",
    )

    revision = service.generate_structured(draft_id, "video_script", "youtube_shorts")

    assert revision["kind"] == "VIDEO_SPEC"
    assert revision["generation_type"] == "video_script"
    assert revision["video_spec"]["rendered_video"] is False
    assert revision["video_spec"]["asset_policy"]["authentic_shopify_image_only"] is True
    assert revision["creative"] is None
    assert revision["active"] is False
    assert revision["estimated_cost_usd"] is not None
    assert revision["actual_cost_usd"] is None
    telemetry = db.scalar(select(AIRequestTelemetry))
    assert telemetry.estimated_cost_usd > Decimal("0")
    assert telemetry.actual_cost_usd is None
    assert provider.calls == 1
    assert db.scalar(select(func.count(PinCreative.id))) == 0
    assert db.scalar(select(func.count(PinPublication.id))) == 0
    db.close()


def test_real_hosted_video_path_uses_reviewable_fallback_without_provider_cost(tmp_path):
    db, product, proposal_service, draft_id, service = prepared(tmp_path, "hosted-video-disabled")
    AISettingsService(proposal_service.session_factory).update(
        enabled=True,
        provider_mode="hosted_paid",
    )

    revision = service.generate_structured(draft_id, "video_script", "youtube_shorts")

    telemetry = db.scalar(select(AIRequestTelemetry))
    assert revision["kind"] == "VIDEO_SPEC"
    assert revision["generation_mode"] == "deterministic_fallback"
    assert revision["video_spec"]["rendered_video"] is False
    assert revision["estimated_cost_usd"] is None
    assert revision["actual_cost_usd"] is None
    assert telemetry.provider == "deterministic"
    assert telemetry.model == "none"
    assert telemetry.estimated_cost_usd is None
    assert db.scalar(select(func.count(PinPublication.id))) == 0
    db.close()


def test_hosted_content_and_storyboard_keep_estimates_separate_from_actual_cost(tmp_path):
    for generation_type in ("content_variant", "storyboard"):
        db, product, proposal_service, draft_id, service = prepared(tmp_path / generation_type, generation_type)
        if generation_type == "content_variant":
            payload = {
                "headline": product.title,
                "title": product.title,
                "description": f"Explore {product.title} using authentic catalog details.",
                "cta": "Explore",
                "board_description": f"Explore {product.title}.",
                "social_post": f"Explore {product.title}.",
                "hooks": [product.title],
                "keywords": [],
            }
        else:
            payload = {
                "concept": f"Editorial catalog view for {product.title}",
                "hook": product.title,
                "script": f"Explore {product.title} using the authentic catalog image.",
                "caption": product.title,
                "overlay_text": [product.title],
                "cta": "Explore",
                "scenes": [{
                    "index": 1,
                    "duration_seconds": 3,
                    "visual": "Authentic Shopify product image in center frame.",
                    "voiceover": product.title,
                    "overlay": product.title,
                }],
            }
        provider = FakeTextProvider(payload)
        provider.name = "openai"
        provider.model = "gpt-4o-mini"
        if generation_type == "storyboard":
            service.video_provider_factory = lambda _, provider=provider: provider
        else:
            service.text_provider_factory = lambda _, provider=provider: provider
        AISettingsService(proposal_service.session_factory).update(
            enabled=True,
            provider_mode="hosted_paid",
        )

        revision = service.generate_structured(draft_id, generation_type)
        telemetry = db.scalar(select(AIRequestTelemetry))

        assert revision["estimated_cost_usd"] is not None
        assert revision["actual_cost_usd"] is None
        assert telemetry.estimated_cost_usd > Decimal("0")
        assert telemetry.actual_cost_usd is None
        assert telemetry.prompt_tokens == 40
        assert telemetry.completion_tokens == 80
        assert telemetry.total_tokens == 120
        expected_schema_name = "video_spec" if generation_type == "storyboard" else "content_variant"
        assert provider.schemas[0][0] == expected_schema_name
        assert provider.schemas[0][1]["additionalProperties"] is False
        db.close()
