from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, Enum, ForeignKey, Index, Integer, JSON, Numeric, String, Text,
    UniqueConstraint, func, text
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def uuid_str() -> str:
    return str(uuid.uuid4())


class DraftStatus(str, enum.Enum):
    GENERATED = "GENERATED"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class PublicationStatus(str, enum.Enum):
    APPROVED = "APPROVED"
    SCHEDULED = "SCHEDULED"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    PUBLISH_FAILED = "PUBLISH_FAILED"
    PUBLISH_UNKNOWN = "PUBLISH_UNKNOWN"
    CANCELLED = "CANCELLED"


class Store(Base):
    __tablename__ = "stores"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    shop_domain: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    market: Mapped[str] = mapped_column(String(20), default="US")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Product(Base):
    __tablename__ = "products"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    shopify_product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    handle: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    vendor: Mapped[str | None] = mapped_column(String(255))
    product_type: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE")
    product_url: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    collections: Mapped[list] = mapped_column(JSON, default=list)
    shopify_data: Mapped[dict] = mapped_column(JSON, default=dict)
    inventory_total: Mapped[int] = mapped_column(Integer, default=0)
    price_min: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    compare_at_min: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    manual_priority: Mapped[int] = mapped_column(Integer, default=0)
    excluded_from_editorial: Mapped[bool] = mapped_column(Boolean, default=False)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    shopify_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    shopify_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    __table_args__ = (UniqueConstraint("store_id", "shopify_product_id", name="uq_product_shopify"),)


class ProductIntelligence(Base):
    __tablename__ = "product_intelligence"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    product_id: Mapped[str] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), unique=True, index=True
    )
    brand: Mapped[str | None] = mapped_column(String(255))
    audience: Mapped[str | None] = mapped_column(String(100))
    designer: Mapped[str | None] = mapped_column(String(255))
    niche: Mapped[str | None] = mapped_column(String(255))
    arabian_classification: Mapped[str | None] = mapped_column(String(100))
    fragrance_family: Mapped[str | None] = mapped_column(String(255))
    fragrance_notes: Mapped[list] = mapped_column(JSON, default=list)
    concentration: Mapped[str | None] = mapped_column(String(100))
    size: Mapped[str | None] = mapped_column(String(100))
    price_band: Mapped[str | None] = mapped_column(String(50))
    gift_suitability: Mapped[str | None] = mapped_column(String(50))
    season: Mapped[str | None] = mapped_column(String(100))
    occasion: Mapped[str | None] = mapped_column(String(100))
    image_quality: Mapped[float] = mapped_column(Numeric(4, 3), default=0)
    image_available: Mapped[bool] = mapped_column(Boolean, default=False)
    inventory_eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    eligibility_score: Mapped[float] = mapped_column(Numeric(6, 2), default=0)
    eligibility_status: Mapped[str] = mapped_column(String(30), default="INELIGIBLE")
    eligibility_reasons: Mapped[list] = mapped_column(JSON, default=list)
    normalization_status: Mapped[str] = mapped_column(String(30), default="UNKNOWN")
    normalized_data: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CatalogSyncJob(Base):
    __tablename__ = "catalog_sync_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="QUEUED", index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    total_seen: Mapped[int] = mapped_column(Integer, default=0)
    products_imported: Mapped[int] = mapped_column(Integer, default=0)
    products_updated: Mapped[int] = mapped_column(Integer, default=0)
    products_failed: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    provider_operation_id: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        Index(
            "uq_catalog_sync_active",
            "store_id",
            unique=True,
            postgresql_where=text("status IN ('QUEUED', 'RUNNING')"),
            sqlite_where=text("status IN ('QUEUED', 'RUNNING')"),
        ),
    )


class ProductVariant(Base):
    __tablename__ = "product_variants"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    shopify_variant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sku: Mapped[str | None] = mapped_column(String(255), index=True)
    title: Mapped[str | None] = mapped_column(String(500))
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    compare_at_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    inventory_quantity: Mapped[int] = mapped_column(Integer, default=0)
    available: Mapped[bool] = mapped_column(Boolean, default=True)


class ProductImage(Base):
    __tablename__ = "product_images"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    shopify_media_id: Mapped[str | None] = mapped_column(String(64))
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    alt_text: Mapped[str | None] = mapped_column(Text)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    source_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    perceptual_hash: Mapped[str | None] = mapped_column(String(128), index=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    editorial_eligible: Mapped[bool] = mapped_column(Boolean, default=True)


class Board(Base):
    __tablename__ = "boards"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    pinterest_board_id: Mapped[str | None] = mapped_column(String(80), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    rules: Mapped[dict] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (UniqueConstraint("store_id", "slug", name="uq_board_slug"),)


class ContentAngle(Base):
    __tablename__ = "content_angles"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    rules: Mapped[dict] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class KeywordCluster(Base):
    __tablename__ = "keyword_clusters"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    key: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    intent: Mapped[str | None] = mapped_column(String(100))


class CreativeTemplate(Base):
    __tablename__ = "creative_templates"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    key: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    renderer: Mapped[str] = mapped_column(String(50), default="satori")
    definition: Mapped[dict] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (UniqueConstraint("key", "version", name="uq_template_version"),)


class Campaign(Base):
    __tablename__ = "campaigns"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    utm_campaign: Mapped[str] = mapped_column(String(255), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (UniqueConstraint("store_id", "slug", name="uq_campaign_slug"),)


class PinConcept(Base):
    __tablename__ = "pin_concepts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    content_angle_id: Mapped[str] = mapped_column(ForeignKey("content_angles.id"), index=True)
    keyword_cluster_id: Mapped[str | None] = mapped_column(ForeignKey("keyword_clusters.id"), index=True)
    board_id: Mapped[str | None] = mapped_column(ForeignKey("boards.id"), index=True)
    campaign_id: Mapped[str | None] = mapped_column(ForeignKey("campaigns.id"), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    rationale: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PinDraft(Base):
    __tablename__ = "pin_drafts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    concept_id: Mapped[str] = mapped_column(ForeignKey("pin_concepts.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    alt_text: Mapped[str] = mapped_column(Text, nullable=False)
    destination_url: Mapped[str] = mapped_column(Text, nullable=False)
    utm_url: Mapped[str] = mapped_column(Text, nullable=False)
    text_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[DraftStatus] = mapped_column(Enum(DraftStatus), default=DraftStatus.GENERATED)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("concept_id", "version", name="uq_draft_version"),)


class PinCreative(Base):
    __tablename__ = "pin_creatives"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    draft_id: Mapped[str] = mapped_column(ForeignKey("pin_drafts.id", ondelete="CASCADE"), index=True)
    template_id: Mapped[str] = mapped_column(ForeignKey("creative_templates.id"), index=True)
    source_image_id: Mapped[str] = mapped_column(ForeignKey("product_images.id"), index=True)
    rendered_url: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    perceptual_hash: Mapped[str | None] = mapped_column(String(128), index=True)
    creative_fingerprint: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    width: Mapped[int] = mapped_column(Integer, default=1000)
    height: Mapped[int] = mapped_column(Integer, default=1500)
    render_status: Mapped[str] = mapped_column(String(30), default="PENDING", index=True)
    render_error: Mapped[str | None] = mapped_column(Text)
    render_spec: Mapped[dict] = mapped_column(JSON, default=dict)
    rendered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    render_duration_ms: Mapped[int | None] = mapped_column(Integer)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AISettings(Base):
    __tablename__ = "ai_settings"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    provider_mode: Mapped[str] = mapped_column(String(30), default="disabled", nullable=False)
    decorative_backgrounds_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    local_base_url: Mapped[str] = mapped_column(String(500), default="http://127.0.0.1:11434", nullable=False)
    local_model: Mapped[str] = mapped_column(String(120), default="llama3.2:3b", nullable=False)
    hosted_model: Mapped[str] = mapped_column(String(120), default="gpt-5.6-luna", nullable=False)
    image_model: Mapped[str] = mapped_column(String(120), default="gpt-image-2", nullable=False)
    video_model: Mapped[str] = mapped_column(String(120), default="gpt-4o-mini", nullable=False)
    request_timeout_seconds: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    daily_budget_usd: Mapped[Decimal] = mapped_column(Numeric(14, 8), default=Decimal("1.0"), nullable=False)
    monthly_budget_usd: Mapped[Decimal] = mapped_column(Numeric(14, 8), default=Decimal("10.0"), nullable=False)
    per_request_cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 8), default=Decimal("0.25"), nullable=False)
    pricing_metadata: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ContentRevision(Base):
    __tablename__ = "content_revisions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    draft_id: Mapped[str] = mapped_column(ForeignKey("pin_drafts.id", ondelete="CASCADE"), index=True)
    parent_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("content_revisions.id", ondelete="SET NULL"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    revision_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="REVIEW", nullable=False)
    headline: Mapped[str] = mapped_column(String(500), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    alt_text: Mapped[str] = mapped_column(Text, nullable=False)
    cta: Mapped[str] = mapped_column(String(255), nullable=False)
    content_angle: Mapped[str] = mapped_column(String(255), nullable=False)
    content_angle_key: Mapped[str] = mapped_column(String(120), nullable=False)
    creative_template: Mapped[str] = mapped_column(String(255), nullable=False)
    creative_template_key: Mapped[str] = mapped_column(String(120), nullable=False)
    destination_url: Mapped[str] = mapped_column(Text, nullable=False)
    utm_url: Mapped[str] = mapped_column(Text, nullable=False)
    keywords: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    facts_used: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    warnings: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    missing_facts: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    unsupported_claims: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    provenance: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    text_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    creative_fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    creative_id: Mapped[str | None] = mapped_column(ForeignKey("pin_creatives.id", ondelete="SET NULL"), index=True)
    source_image_id: Mapped[str] = mapped_column(ForeignKey("product_images.id"), index=True)
    provider_mode: Mapped[str] = mapped_column(String(30), nullable=False)
    generation_mode: Mapped[str] = mapped_column(String(40), nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    generation_type: Mapped[str] = mapped_column(String(40), default="copy", nullable=False)
    intended_channel: Mapped[str] = mapped_column(String(40), default="pinterest", nullable=False)
    content_payload: Mapped[dict | None] = mapped_column(JSON)
    video_spec: Mapped[dict | None] = mapped_column(JSON)
    background_asset_id: Mapped[str | None] = mapped_column(
        ForeignKey("ai_generated_assets.id", ondelete="SET NULL"), index=True
    )
    background_asset: Mapped["AIGeneratedAsset | None"] = relationship(foreign_keys=[background_asset_id])
    ai_telemetry_id: Mapped[str | None] = mapped_column(String(36), index=True)
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 8))
    actual_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 8))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint("draft_id", "version", name="uq_content_revision_version"),
    )


class ContentVersionSelection(Base):
    __tablename__ = "content_version_selections"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    draft_id: Mapped[str] = mapped_column(
        ForeignKey("pin_drafts.id", ondelete="CASCADE"), unique=True, index=True
    )
    revision_id: Mapped[str] = mapped_column(
        ForeignKey("content_revisions.id", ondelete="CASCADE"), index=True
    )
    selected_by: Mapped[str] = mapped_column(String(255), nullable=False)
    selected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AIRequestTelemetry(Base):
    __tablename__ = "ai_request_telemetry"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    draft_id: Mapped[str | None] = mapped_column(ForeignKey("pin_drafts.id", ondelete="SET NULL"), index=True)
    provider: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    operation: Mapped[str] = mapped_column(String(40), nullable=False)
    request_type: Mapped[str] = mapped_column(String(40), default="generation", nullable=False)
    generation_type: Mapped[str] = mapped_column(String(40), default="copy", nullable=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(80))
    fallback_reason: Mapped[str | None] = mapped_column(String(120))
    validation_failure_reason: Mapped[str | None] = mapped_column(String(120))
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 8))
    actual_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 8))
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class AIGeneratedAsset(Base):
    __tablename__ = "ai_generated_assets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    draft_id: Mapped[str] = mapped_column(ForeignKey("pin_drafts.id", ondelete="CASCADE"), index=True)
    asset_type: Mapped[str] = mapped_column(String(40), nullable=False)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="REVIEW", nullable=False)
    storage_path: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(String(80))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provenance: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class PinApproval(Base):
    __tablename__ = "pin_approvals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    draft_id: Mapped[str] = mapped_column(ForeignKey("pin_drafts.id", ondelete="CASCADE"), index=True)
    revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("content_revisions.id", ondelete="RESTRICT"), index=True
    )
    creative_id: Mapped[str | None] = mapped_column(
        ForeignKey("pin_creatives.id", ondelete="RESTRICT"), index=True
    )
    approved_version_id: Mapped[str | None] = mapped_column(String(36), index=True)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    decided_by: Mapped[str] = mapped_column(String(255), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PinPublication(Base):
    __tablename__ = "pin_publications"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    draft_id: Mapped[str] = mapped_column(ForeignKey("pin_drafts.id", ondelete="CASCADE"), index=True)
    revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("content_revisions.id", ondelete="RESTRICT"), index=True
    )
    creative_id: Mapped[str] = mapped_column(ForeignKey("pin_creatives.id"), index=True)
    approval_id: Mapped[str | None] = mapped_column(
        ForeignKey("pin_approvals.id", ondelete="RESTRICT"), index=True
    )
    source_image_id: Mapped[str | None] = mapped_column(
        ForeignKey("product_images.id", ondelete="RESTRICT"), index=True
    )
    template_id: Mapped[str | None] = mapped_column(
        ForeignKey("creative_templates.id", ondelete="RESTRICT"), index=True
    )
    template_key: Mapped[str | None] = mapped_column(String(120))
    template_version: Mapped[int | None] = mapped_column(Integer)
    text_fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    creative_fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    board_id: Mapped[str | None] = mapped_column(ForeignKey("boards.id"), index=True)
    pinterest_board_id: Mapped[str | None] = mapped_column(String(80), index=True)
    integration_account_id: Mapped[str | None] = mapped_column(
        ForeignKey("integration_accounts.id", ondelete="RESTRICT"), index=True
    )
    pinterest_connection_id: Mapped[str | None] = mapped_column(
        ForeignKey("pinterest_connections.id", ondelete="RESTRICT"), index=True
    )
    pinterest_board_record_id: Mapped[str | None] = mapped_column(
        ForeignKey("pinterest_boards.id", ondelete="RESTRICT"), index=True
    )
    pinterest_board_id_snapshot: Mapped[str | None] = mapped_column(String(255))
    title_snapshot: Mapped[str | None] = mapped_column(Text)
    description_snapshot: Mapped[str | None] = mapped_column(Text)
    alt_text_snapshot: Mapped[str | None] = mapped_column(Text)
    media_url_snapshot: Mapped[str | None] = mapped_column(Text)
    destination_url: Mapped[str | None] = mapped_column(Text)
    utm_url: Mapped[str | None] = mapped_column(Text)
    publication_fingerprint: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    status: Mapped[PublicationStatus] = mapped_column(Enum(PublicationStatus), default=PublicationStatus.APPROVED)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    attempt_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pinterest_pin_id: Mapped[str | None] = mapped_column(String(80), index=True)
    provider_response: Mapped[dict] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PublicationAttempt(Base):
    __tablename__ = "publication_attempts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    publication_id: Mapped[str] = mapped_column(ForeignKey("pin_publications.id", ondelete="CASCADE"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    request_fingerprint: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_pin_id: Mapped[str | None] = mapped_column(String(255))
    error_code: Mapped[str | None] = mapped_column(String(100))
    safe_response_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("publication_id", "attempt_number", name="uq_publication_attempt_number"),)


class PublicationDispatchAuthorization(Base):
    """Durable, server-derived human authorization for a future manual dispatch.

    ``authorized_by`` must be derived from the authenticated admin principal by
    the future API/service layer.  It is not a client-owned JSON field.
    """

    __tablename__ = "publication_dispatch_authorizations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    publication_id: Mapped[str] = mapped_column(
        ForeignKey("pin_publications.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    authorized_by: Mapped[str] = mapped_column(String(255), nullable=False)
    authorized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    publication_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    quality_policy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    quality_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    readiness_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    duplicate_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    confirmation_text_version: Mapped[str] = mapped_column(String(80), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoke_reason: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE', 'CONSUMED', 'REVOKED', 'EXPIRED')",
            name="ck_publication_dispatch_authorization_status",
        ),
        Index(
            "uq_publication_dispatch_authorizations_active",
            "publication_id",
            unique=True,
            sqlite_where=text("status = 'ACTIVE'"),
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        Index("ix_publication_dispatch_authorizations_expires_at", "expires_at"),
        Index("ix_publication_dispatch_authorizations_status", "status"),
        Index("ix_publication_dispatch_authorizations_authorized_at", "authorized_at"),
    )


class PublicationReconciliationEvent(Base):
    """Safe operator audit event for future explicit PUBLISH_UNKNOWN handling."""

    __tablename__ = "publication_reconciliation_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    publication_id: Mapped[str] = mapped_column(
        ForeignKey("pin_publications.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    attempt_id: Mapped[str | None] = mapped_column(
        ForeignKey("publication_attempts.id", ondelete="RESTRICT"), index=True
    )
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    previous_status: Mapped[str] = mapped_column(String(30), nullable=False)
    new_status: Mapped[str] = mapped_column(String(30), nullable=False)
    provider_pin_id: Mapped[str | None] = mapped_column(String(255))
    reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True, nullable=False)
    __table_args__ = (
        CheckConstraint(
            "action IN ('PROVIDER_PIN_CONFIRMED', 'CANCELLED_UNKNOWN')",
            name="ck_publication_reconciliation_action",
        ),
        CheckConstraint(
            "previous_status = 'PUBLISH_UNKNOWN'",
            name="ck_publication_reconciliation_previous_status",
        ),
        CheckConstraint(
            "((action = 'PROVIDER_PIN_CONFIRMED' AND new_status = 'PUBLISHED') OR "
            "(action = 'CANCELLED_UNKNOWN' AND new_status = 'CANCELLED'))",
            name="ck_publication_reconciliation_transition",
        ),
    )


class PinterestOAuthState(Base):
    __tablename__ = "pinterest_oauth_states"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    initiated_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    redirect_after: Mapped[str | None] = mapped_column(Text)


class PinterestConnection(Base):
    __tablename__ = "pinterest_connections"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    provider: Mapped[str] = mapped_column(String(30), default="pinterest", nullable=False)
    external_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    username: Mapped[str | None] = mapped_column(String(255))
    account_type: Mapped[str | None] = mapped_column(String(80))
    profile_image_url: Mapped[str | None] = mapped_column(Text)
    granted_scopes: Mapped[list] = mapped_column(JSON, default=list)
    access_token_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refresh_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    token_type: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(30), default="CONNECTED", index=True)
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    boards_last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PinterestBoard(Base):
    __tablename__ = "pinterest_boards"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    connection_id: Mapped[str] = mapped_column(ForeignKey("pinterest_connections.id", ondelete="CASCADE"), index=True, nullable=False)
    external_board_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    privacy: Mapped[str | None] = mapped_column(String(40))
    owner_username: Mapped[str | None] = mapped_column(String(255))
    pin_count: Mapped[int | None] = mapped_column(Integer)
    follower_count: Mapped[int | None] = mapped_column(Integer)
    collaborator_count: Mapped[int | None] = mapped_column(Integer)
    is_ads_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    image_cover_url: Mapped[str | None] = mapped_column(Text)
    board_pins_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_eligible: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    routing_label: Mapped[str | None] = mapped_column(String(120))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    __table_args__ = (UniqueConstraint("connection_id", "external_board_id", name="uq_pinterest_board_identity"),)


class PinterestBoardSection(Base):
    __tablename__ = "pinterest_board_sections"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    board_id: Mapped[str] = mapped_column(ForeignKey("pinterest_boards.id", ondelete="CASCADE"), index=True, nullable=False)
    external_section_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    __table_args__ = (UniqueConstraint("board_id", "external_section_id", name="uq_pinterest_section_identity"),)


class IntegrationAccount(Base):
    __tablename__ = "integration_accounts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    external_account_id: Mapped[str | None] = mapped_column(String(255))
    encrypted_credentials: Mapped[str | None] = mapped_column(Text)
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    access_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refresh_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (UniqueConstraint("store_id", "provider", name="uq_store_provider"),)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    entity_type: Mapped[str] = mapped_column(String(120), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64), index=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
