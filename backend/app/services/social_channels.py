"""Platform-neutral social content contracts and internal channel registry.

This module contains no provider SDK calls, credentials, publishing side effects,
or persistence. It describes which social workflows the product can prepare and
review, independently from which providers are currently connected for delivery.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from enum import Enum
from typing import Any, Mapping, Protocol


class ChannelStatus(str, Enum):
    INTERNAL_PREVIEW = "INTERNAL_PREVIEW"
    CONTENT_READY = "CONTENT_READY"
    NOT_CONNECTED = "NOT_CONNECTED"


class AccountStatus(str, Enum):
    INTERNAL = "INTERNAL"
    NOT_CONNECTED = "NOT_CONNECTED"


@dataclass(frozen=True)
class ChannelMediaRequirement:
    media_kind: str
    accepted_formats: tuple[str, ...]
    aspect_ratios: tuple[str, ...]
    min_width: int | None
    min_height: int | None
    max_duration_seconds: int | None
    catalog_source_required: bool
    notes: str


@dataclass(frozen=True)
class PlatformContentVariant:
    key: str
    label: str
    required_text_fields: tuple[str, ...]
    media_requirements: tuple[ChannelMediaRequirement, ...]
    destination_required: bool = True


@dataclass(frozen=True)
class ChannelAccount:
    channel_key: str
    status: AccountStatus
    mode: str
    external_account_id: str | None = None


@dataclass(frozen=True)
class ChannelFeatureSet:
    content_preview: bool
    content_generation: bool
    review: bool
    account_connection: bool
    publishing: bool
    scheduling: bool
    analytics: bool


@dataclass(frozen=True)
class ChannelDescriptor:
    key: str
    label: str
    status: ChannelStatus
    future: bool
    adapter_key: str | None
    account: ChannelAccount
    capabilities: ChannelFeatureSet
    variants: tuple[PlatformContentVariant, ...]
    capability_summary: str


@dataclass(frozen=True)
class MediaAsset:
    url: str
    media_kind: str
    width: int | None = None
    height: int | None = None
    source_kind: str = "unknown"
    source_id: str | None = None
    checksum: str | None = None


@dataclass(frozen=True)
class SocialContent:
    channel_key: str
    variant_key: str
    source_id: str
    text: Mapping[str, str]
    media: tuple[MediaAsset, ...]
    destination_url: str | None


class ChannelAdapter(Protocol):
    @property
    def descriptor(self) -> ChannelDescriptor: ...

    def validate_content(self, content: SocialContent) -> tuple[str, ...]: ...


PINTEREST_VARIANT = PlatformContentVariant(
    key="pin",
    label="Pinterest Pin",
    required_text_fields=("title", "description", "alt_text"),
    media_requirements=(
        ChannelMediaRequirement(
            media_kind="image",
            accepted_formats=("PNG", "JPEG", "WEBP"),
            aspect_ratios=("2:3",),
            min_width=1000,
            min_height=1500,
            max_duration_seconds=None,
            catalog_source_required=True,
            notes="Uses the existing authentic Shopify product source or verified rendered creative.",
        ),
    ),
)

CHANNEL_VARIANTS = {
    "instagram": (
        PlatformContentVariant(
            key="feed_post",
            label="Instagram Feed Post",
            required_text_fields=("caption",),
            media_requirements=(
                ChannelMediaRequirement(
                    media_kind="image_or_video",
                    accepted_formats=("JPEG", "PNG", "MP4"),
                    aspect_ratios=("1:1", "4:5", "1.91:1"),
                    min_width=1080,
                    min_height=1080,
                    max_duration_seconds=None,
                    catalog_source_required=False,
                    notes="Review-generation metadata is implemented; account connection and publishing are not yet implemented.",
                ),
            ),
        ),
    ),
    "facebook": (
        PlatformContentVariant(
            key="page_post",
            label="Facebook Page Post",
            required_text_fields=("message",),
            media_requirements=(
                ChannelMediaRequirement(
                    media_kind="image_or_video",
                    accepted_formats=("JPEG", "PNG", "MP4"),
                    aspect_ratios=("1.91:1", "1:1"),
                    min_width=1200,
                    min_height=630,
                    max_duration_seconds=None,
                    catalog_source_required=False,
                    notes="Review-generation metadata is implemented; account connection and publishing are not yet implemented.",
                ),
            ),
        ),
    ),
    "linkedin": (
        PlatformContentVariant(
            key="page_post",
            label="LinkedIn Page Post",
            required_text_fields=("commentary",),
            media_requirements=(
                ChannelMediaRequirement(
                    media_kind="image_or_video",
                    accepted_formats=("JPEG", "PNG", "MP4"),
                    aspect_ratios=("1.91:1", "1:1"),
                    min_width=1200,
                    min_height=627,
                    max_duration_seconds=None,
                    catalog_source_required=False,
                    notes="Review-generation metadata is implemented; account connection and publishing are not yet implemented.",
                ),
            ),
        ),
    ),
    "tiktok": (
        PlatformContentVariant(
            key="video_post",
            label="TikTok Video",
            required_text_fields=("caption",),
            media_requirements=(
                ChannelMediaRequirement(
                    media_kind="video",
                    accepted_formats=("MP4", "MOV"),
                    aspect_ratios=("9:16",),
                    min_width=1080,
                    min_height=1920,
                    max_duration_seconds=600,
                    catalog_source_required=False,
                    notes="Review-generation metadata is implemented; account connection and publishing are not yet implemented.",
                ),
            ),
        ),
    ),
    "youtube": (
        PlatformContentVariant(
            key="video",
            label="YouTube Video",
            required_text_fields=("title", "description"),
            media_requirements=(
                ChannelMediaRequirement(
                    media_kind="video",
                    accepted_formats=("MP4", "MOV"),
                    aspect_ratios=("16:9", "9:16"),
                    min_width=1280,
                    min_height=720,
                    max_duration_seconds=None,
                    catalog_source_required=False,
                    notes="YouTube is the canonical UI identity; existing youtube_shorts review revisions remain backward-compatible.",
                ),
            ),
        ),
        PlatformContentVariant(
            key="thumbnail",
            label="YouTube Thumbnail",
            required_text_fields=(),
            media_requirements=(
                ChannelMediaRequirement(
                    media_kind="image",
                    accepted_formats=("JPEG", "PNG"),
                    aspect_ratios=("16:9",),
                    min_width=1280,
                    min_height=720,
                    max_duration_seconds=None,
                    catalog_source_required=False,
                    notes="Review-generation metadata only; no YouTube publishing adapter is connected.",
                ),
            ),
            destination_required=False,
        ),
    ),
}


class PinterestInternalPreviewAdapter:
    """Compatibility adapter for the existing Pinterest proposal/creative records."""

    @property
    def descriptor(self) -> ChannelDescriptor:
        return ChannelDescriptor(
            key="pinterest",
            label="Pinterest",
            status=ChannelStatus.INTERNAL_PREVIEW,
            future=False,
            adapter_key="pinterest-internal-preview",
            account=ChannelAccount(
                channel_key="pinterest",
                status=AccountStatus.INTERNAL,
                mode="internal_preview",
            ),
            capabilities=ChannelFeatureSet(
                content_preview=True,
                content_generation=True,
                review=True,
                account_connection=True,
                publishing=False,
                scheduling=False,
                analytics=False,
            ),
            variants=(PINTEREST_VARIANT,),
            capability_summary="Pinterest content generation, review, account connection, scheduling, and publishing infrastructure are implemented; live execution still follows the production gate and authorization rules.",
        )

    def validate_content(self, content: SocialContent) -> tuple[str, ...]:
        errors: list[str] = []
        if content.channel_key != "pinterest":
            errors.append("Content channel does not match the Pinterest adapter.")
        if content.variant_key != "pin":
            errors.append("Pinterest internal preview supports the pin variant only.")
        for field in PINTEREST_VARIANT.required_text_fields:
            if not content.text.get(field, "").strip():
                errors.append(f"Pinterest content is missing required field: {field}.")
        if PINTEREST_VARIANT.destination_required and not content.destination_url:
            errors.append("Pinterest content requires a destination URL.")
        if len(content.media) != 1:
            errors.append("Pinterest internal preview requires exactly one image.")
        else:
            asset = content.media[0]
            requirement = PINTEREST_VARIANT.media_requirements[0]
            if asset.media_kind != requirement.media_kind:
                errors.append("Pinterest internal preview requires image media.")
            if asset.source_kind not in {"shopify_catalog", "rendered_preview"}:
                errors.append("Pinterest content must use an authentic catalog or existing rendered preview source.")
            if asset.width is None:
                errors.append("Pinterest media width is required for validation.")
            elif asset.width < requirement.min_width:
                errors.append("Pinterest preview image is narrower than the supported canvas.")
            if asset.height is None:
                errors.append("Pinterest media height is required for validation.")
            elif asset.height < requirement.min_height:
                errors.append("Pinterest preview image is shorter than the supported canvas.")
        return tuple(errors)

    def from_existing_proposal(self, proposal: Mapping[str, Any]) -> SocialContent:
        creative = proposal.get("creative") or {}
        image_spec = (creative.get("specification") or {}).get("image") or {}
        verified_render = (
            creative.get("status") == "RENDERED"
            and bool(creative.get("image_url"))
            and bool(creative.get("sha256"))
            and bool(image_spec.get("provenance_url"))
            and bool(image_spec.get("checksum_sha256"))
            and isinstance(creative.get("width"), int)
            and creative["width"] > 0
            and isinstance(creative.get("height"), int)
            and creative["height"] > 0
        )
        width = creative.get("width") if verified_render else None
        height = creative.get("height") if verified_render else None
        return SocialContent(
            channel_key="pinterest",
            variant_key="pin",
            source_id=str(proposal.get("id") or proposal.get("concept_id") or ""),
            text={
                "title": str(proposal.get("title") or ""),
                "description": str(proposal.get("description") or ""),
                "alt_text": str(proposal.get("alt_text") or proposal.get("product_title") or ""),
            },
            media=(
                MediaAsset(
                    url=str(creative.get("image_url") if verified_render else proposal.get("image_url") or ""),
                    media_kind="image",
                    width=int(width) if width is not None else None,
                    height=int(height) if height is not None else None,
                    source_kind="rendered_preview" if verified_render else "shopify_catalog",
                    source_id=str(creative.get("id") or proposal.get("product_id") or ""),
                    checksum=creative.get("sha256") if verified_render else None,
                ),
            ),
            destination_url=str(proposal.get("canonical_url") or ""),
        )


CHANNEL_ADAPTERS: dict[str, ChannelAdapter] = {
    "pinterest": PinterestInternalPreviewAdapter(),
}

_CHANNEL_LABELS = {
    "instagram": "Instagram",
    "facebook": "Facebook",
    "linkedin": "LinkedIn",
    "tiktok": "TikTok",
    "youtube": "YouTube",
}


def channel_descriptors(publishing_enabled: bool = False) -> tuple[ChannelDescriptor, ...]:
    pinterest_base = CHANNEL_ADAPTERS["pinterest"].descriptor
    pinterest = replace(
        pinterest_base,
        status=ChannelStatus.CONTENT_READY,
        capabilities=replace(
            pinterest_base.capabilities,
            publishing=publishing_enabled,
            scheduling=publishing_enabled,
        ),
    )
    review_ready = [
        ChannelDescriptor(
            key=key,
            label=label,
            status=ChannelStatus.CONTENT_READY,
            future=False,
            adapter_key=None,
            account=ChannelAccount(
                channel_key=key,
                status=AccountStatus.NOT_CONNECTED,
                mode="content_ready_no_connection",
            ),
            capabilities=ChannelFeatureSet(
                content_preview=True,
                content_generation=True,
                review=True,
                account_connection=False,
                publishing=False,
                scheduling=False,
                analytics=False,
            ),
            variants=CHANNEL_VARIANTS[key],
            capability_summary="Content generation and review are available. Account connection, scheduling, publishing, and analytics are not yet implemented for this channel.",
        )
        for key, label in _CHANNEL_LABELS.items()
    ]
    return tuple([pinterest, *review_ready])


def channel_capability_payload(publishing_enabled: bool = False) -> dict[str, Any]:
    return {
        "publishing_enabled": publishing_enabled,
        "channels": [asdict(descriptor) for descriptor in channel_descriptors(publishing_enabled)],
    }
