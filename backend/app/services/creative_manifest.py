from dataclasses import dataclass


@dataclass(frozen=True)
class CreativeManifest:
    product_id: str
    source_image_url: str
    source_is_catalog_image: bool
    template_key: str
    template_version: int
    headline: str
    subheadline: str | None = None
    width: int = 1000
    height: int = 1500


class CreativePolicyError(ValueError):
    pass


def validate_creative_manifest(manifest: CreativeManifest) -> None:
    if manifest.width != 1000 or manifest.height != 1500:
        raise CreativePolicyError("Phase 1 Pinterest creatives must render at 1000x1500")
    if not manifest.source_image_url.startswith(("https://", "http://")):
        raise CreativePolicyError("A product creative requires a source image URL")
    if not manifest.source_is_catalog_image:
        raise CreativePolicyError("Specific-product Pins must use authentic catalog product imagery")
    if not manifest.headline.strip():
        raise CreativePolicyError("Creative headline cannot be empty")
