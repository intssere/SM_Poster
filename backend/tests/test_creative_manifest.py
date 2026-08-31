import pytest
from app.services.creative_manifest import CreativeManifest, CreativePolicyError, validate_creative_manifest


def valid(**kwargs):
    data = dict(
        product_id="p1",
        source_image_url="https://cdn.shopify.com/example.jpg",
        source_is_catalog_image=True,
        template_key="luxury-spotlight",
        template_version=1,
        headline="Example Fragrance",
    )
    data.update(kwargs)
    return CreativeManifest(**data)


def test_real_catalog_image_is_required():
    with pytest.raises(CreativePolicyError):
        validate_creative_manifest(valid(source_is_catalog_image=False))


def test_default_pin_canvas_is_valid():
    validate_creative_manifest(valid())
