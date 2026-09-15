from fastapi.testclient import TestClient

from app.main import app
from app.schemas.pins import RegenerationRequest
from app.services.ai_creative_generation import CHANNELS as AI_GENERATION_CHANNELS
from app.services.social_channels import (
    AccountStatus,
    ChannelStatus,
    MediaAsset,
    PinterestInternalPreviewAdapter,
    SocialContent,
    channel_capability_payload,
)


def test_registry_exposes_six_first_class_content_ready_channels():
    payload = channel_capability_payload()

    assert payload["publishing_enabled"] is False
    assert [channel["key"] for channel in payload["channels"]] == [
        "pinterest",
        "instagram",
        "facebook",
        "linkedin",
        "tiktok",
        "youtube",
    ]
    assert all(channel["future"] is False for channel in payload["channels"])
    assert all(channel["status"] == ChannelStatus.CONTENT_READY for channel in payload["channels"])
    assert all(channel["capabilities"]["content_generation"] is True for channel in payload["channels"])
    assert all(channel["capabilities"]["review"] is True for channel in payload["channels"])

    pinterest, *other_channels = payload["channels"]
    assert pinterest["account"]["status"] == AccountStatus.INTERNAL
    assert pinterest["adapter_key"] == "pinterest-internal-preview"
    assert pinterest["capabilities"]["account_connection"] is True
    assert pinterest["capabilities"]["publishing"] is False
    assert pinterest["capabilities"]["scheduling"] is False

    assert all(channel["account"]["status"] == AccountStatus.NOT_CONNECTED for channel in other_channels)
    assert all(channel["adapter_key"] is None for channel in other_channels)
    assert all(channel["capabilities"]["account_connection"] is False for channel in other_channels)
    assert all(channel["capabilities"]["publishing"] is False for channel in other_channels)
    assert all(channel["capabilities"]["scheduling"] is False for channel in other_channels)


def test_capabilities_endpoint_reflects_live_gate_only_for_pinterest(monkeypatch):
    monkeypatch.setenv("PUBLISHING_ENABLED", "true")
    monkeypatch.setenv("AUTH_DISABLED", "true")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    from app.core.config import get_settings

    get_settings.cache_clear()
    response = TestClient(app).get("/api/channels/capabilities")

    assert response.status_code == 200
    payload = response.json()
    assert payload["publishing_enabled"] is True
    pinterest, *other_channels = payload["channels"]
    assert pinterest["capabilities"]["publishing"] is True
    assert pinterest["capabilities"]["scheduling"] is True
    assert all(channel["capabilities"]["publishing"] is False for channel in other_channels)
    assert all(channel["capabilities"]["scheduling"] is False for channel in other_channels)


def test_linkedin_is_review_generation_ready_without_connection_or_publishing():
    request = RegenerationRequest(kind="content_variant", channel="linkedin", count=1)
    assert request.channel == "linkedin"
    assert "linkedin" in AI_GENERATION_CHANNELS

    linkedin = next(channel for channel in channel_capability_payload(True)["channels"] if channel["key"] == "linkedin")
    assert linkedin["capabilities"]["content_generation"] is True
    assert linkedin["capabilities"]["review"] is True
    assert linkedin["capabilities"]["account_connection"] is False
    assert linkedin["capabilities"]["publishing"] is False


def test_content_ready_channels_have_variant_media_requirements_without_fake_provider_adapters():
    payload = channel_capability_payload()

    for channel in payload["channels"][1:]:
        assert channel["adapter_key"] is None
        assert channel["variants"]
        assert all(variant["media_requirements"] for variant in channel["variants"])


def test_pinterest_adapter_validates_existing_content_contract():
    adapter = PinterestInternalPreviewAdapter()
    content = SocialContent(
        channel_key="pinterest",
        variant_key="pin",
        source_id="draft-1",
        text={
            "title": "Authentic Product",
            "description": "View the product.",
            "alt_text": "Authentic product image",
        },
        media=(
            MediaAsset(
                url="/creative.png",
                media_kind="image",
                width=1000,
                height=1500,
                source_kind="rendered_preview",
                source_id="creative-1",
            ),
        ),
        destination_url="https://example.com/product",
    )

    assert adapter.validate_content(content) == ()


def test_pinterest_adapter_rejects_non_catalog_media():
    adapter = PinterestInternalPreviewAdapter()
    content = SocialContent(
        channel_key="pinterest",
        variant_key="pin",
        source_id="draft-1",
        text={"title": "Title", "description": "Description", "alt_text": "Alt"},
        media=(MediaAsset(url="/generated.png", media_kind="image", source_kind="ai_generated"),),
        destination_url="https://example.com/product",
    )

    errors = adapter.validate_content(content)
    assert any("authentic catalog" in error for error in errors)


def test_existing_pinterest_proposal_maps_without_mutating_legacy_shape():
    proposal = {
        "id": "draft-1",
        "product_id": "product-1",
        "product_title": "Authentic Product",
        "title": "Authentic Product | Editorial Pick",
        "description": "View the authentic product.",
        "alt_text": "Authentic Product by Diamond Shelf",
        "canonical_url": "https://example.com/products/authentic",
        "image_url": "https://cdn.shopify.com/authentic.jpg",
        "creative": {
            "id": "creative-1",
            "image_url": "/api/pins/creatives/creative-1/image",
            "status": "RENDERED",
            "sha256": "creative-checksum",
            "width": 1000,
            "height": 1500,
            "specification": {
                "image": {
                    "provenance_url": "https://cdn.shopify.com/authentic.jpg",
                    "checksum_sha256": "source-checksum",
                }
            },
        },
    }

    content = PinterestInternalPreviewAdapter().from_existing_proposal(proposal)

    assert content.source_id == "draft-1"
    assert content.text["alt_text"] == proposal["alt_text"]
    assert content.media[0].source_kind == "rendered_preview"
    assert content.media[0].width == 1000
    assert proposal["creative"]["specification"]["image"]["checksum_sha256"] == "source-checksum"


def test_unverified_legacy_creative_cannot_manufacture_media_compliance():
    proposal = {
        "id": "draft-1",
        "product_id": "product-1",
        "product_title": "Authentic Product",
        "title": "Title",
        "description": "Description",
        "alt_text": "Alt",
        "canonical_url": "https://example.com/product",
        "image_url": "https://cdn.shopify.com/authentic.jpg",
        "creative": {
            "id": "creative-1",
            "image_url": "/unverified-render.png",
            "status": "RENDERED",
            "sha256": "creative-checksum",
            "specification": {
                "image": {
                    "provenance_url": "https://cdn.shopify.com/authentic.jpg",
                    "checksum_sha256": "source-checksum",
                }
            },
        },
    }

    adapter = PinterestInternalPreviewAdapter()
    content = adapter.from_existing_proposal(proposal)

    assert content.media[0].source_kind == "shopify_catalog"
    assert content.media[0].width is None
    assert content.media[0].height is None
    errors = adapter.validate_content(content)
    assert "Pinterest media width is required for validation." in errors
    assert "Pinterest media height is required for validation." in errors
