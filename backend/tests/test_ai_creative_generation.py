import json

from sqlalchemy import func, select

from app.models.domain import (
    AIGeneratedAsset,
    AIRequestTelemetry,
    ContentRevision,
    PinCreative,
    PinDraft,
    PinPublication,
)
from app.services.ai_creative_generation import (
    AICreativeGenerationError,
    AICreativeGenerationService,
    AIGeneratedAssetStorage,
)
from app.services.ai_providers import ImageGenerationResult, TextGenerationResult
from app.services.ai_regeneration import AISettingsService
from app.services.creative_rendering import CreativeRenderService, CreativeStorage

from test_creative_rendering import png
from test_pin_proposals import add_product, setup_service


class FakeImageProvider:
    name = "openai"
    model = "gpt-image-1"

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

    def generate(self, prompt):
        self.calls += 1
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


def test_background_generation_composites_authentic_source_and_preserves_legacy_rows(tmp_path):
    db, product, proposal_service, draft_id, service = prepared(tmp_path, "ai-background")
    provider = FakeImageProvider()
    service.image_provider_factory = lambda _: provider
    AISettingsService(proposal_service.session_factory).update(
        enabled=True, provider_mode="hosted_paid", decorative_backgrounds_enabled=True,
        per_request_cost_usd=0.25,
    )
    original = db.get(PinDraft, draft_id)
    original_state = (original.title, original.description, original.text_fingerprint)

    revision = service.generate_background(draft_id, "quiet_luxury", "instagram")

    db.expire_all()
    asset = db.get(AIGeneratedAsset, revision["background_asset_id"])
    creative = db.get(PinCreative, revision["creative"]["id"])
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
    assert (original.title, original.description, original.text_fingerprint) == original_state
    assert db.scalar(select(func.count(PinPublication.id))) == 0
    db.close()


def test_background_validation_and_per_request_ceiling_fail_without_variants(tmp_path):
    db, product, proposal_service, draft_id, service = prepared(tmp_path, "ai-block")
    settings = AISettingsService(proposal_service.session_factory)
    settings.update(
        enabled=True, provider_mode="hosted_paid", decorative_backgrounds_enabled=True,
        per_request_cost_usd=0.01,
    )
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
    service.video_provider_factory = lambda _: provider
    AISettingsService(proposal_service.session_factory).update(
        enabled=True, provider_mode="local_free",
    )

    revision = service.generate_structured(draft_id, "video_script", "youtube_shorts")

    assert revision["kind"] == "VIDEO_SPEC"
    assert revision["generation_type"] == "video_script"
    assert revision["video_spec"]["rendered_video"] is False
    assert revision["video_spec"]["asset_policy"]["authentic_shopify_image_only"] is True
    assert revision["creative"] is None
    assert revision["active"] is False
    assert provider.calls == 1
    assert db.scalar(select(func.count(PinCreative.id))) == 0
    assert db.scalar(select(func.count(PinPublication.id))) == 0
    db.close()