"""phase0 domain schema

Revision ID: 0001
Revises:
"""
from alembic import op
import sqlalchemy as sa

from app.db.base import Base
from app.models import domain  # noqa: F401

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


PHASE0_TABLES = (
    "stores",
    "products",
    "product_variants",
    "product_images",
    "boards",
    "content_angles",
    "keyword_clusters",
    "creative_templates",
    "campaigns",
    "pin_concepts",
    "pin_drafts",
    "pin_creatives",
    "pin_approvals",
    "pin_publications",
    "integration_accounts",
    "audit_logs",
)

PHASE0_PRODUCT_COLUMNS = (
    "id",
    "store_id",
    "shopify_product_id",
    "handle",
    "title",
    "vendor",
    "product_type",
    "status",
    "product_url",
    "tags",
    "attributes",
    "inventory_total",
    "price_min",
    "compare_at_min",
    "manual_priority",
    "excluded_from_editorial",
    "synced_at",
    "created_at",
    "updated_at",
)

PHASE0_CREATIVE_COLUMNS = (
    "id",
    "draft_id",
    "template_id",
    "source_image_id",
    "rendered_url",
    "sha256",
    "perceptual_hash",
    "creative_fingerprint",
    "width",
    "height",
    "created_at",
)

PHASE0_APPROVAL_COLUMNS = (
    "id", "draft_id", "decision", "decided_by", "note", "created_at",
)

PHASE0_PUBLICATION_COLUMNS = (
    "id", "draft_id", "creative_id", "board_id", "publication_fingerprint",
    "status", "scheduled_for", "attempt_started_at", "pinterest_pin_id",
    "provider_response", "error_code", "error_message", "published_at", "created_at",
)


def phase0_metadata() -> sa.MetaData:
    """Build the immutable Phase 0 schema without later model additions."""
    source = Base.metadata
    metadata = sa.MetaData()
    source.tables["stores"].to_metadata(metadata)

    product = source.tables["products"]
    sa.Table(
        "products",
        metadata,
        *(product.c[name]._copy() for name in PHASE0_PRODUCT_COLUMNS),
        sa.UniqueConstraint("store_id", "shopify_product_id", name="uq_product_shopify"),
    )

    creative = source.tables["pin_creatives"]
    sa.Table(
        "pin_creatives",
        metadata,
        *(creative.c[name]._copy() for name in PHASE0_CREATIVE_COLUMNS),
        sa.UniqueConstraint("creative_fingerprint", name="uq_pin_creative_fingerprint"),
    )

    approval = source.tables["pin_approvals"]
    sa.Table(
        "pin_approvals",
        metadata,
        *(approval.c[name]._copy() for name in PHASE0_APPROVAL_COLUMNS),
    )

    publication = source.tables["pin_publications"]
    sa.Table(
        "pin_publications",
        metadata,
        *(publication.c[name]._copy() for name in PHASE0_PUBLICATION_COLUMNS),
        sa.UniqueConstraint("publication_fingerprint", name="uq_pin_publication_fingerprint"),
    )

    for name in PHASE0_TABLES:
        if name not in {"stores", "products", "pin_creatives", "pin_approvals", "pin_publications"}:
            source.tables[name].to_metadata(metadata)
    return metadata


def upgrade():
    bind = op.get_bind()
    phase0_metadata().create_all(bind=bind)


def downgrade():
    bind = op.get_bind()
    phase0_metadata().drop_all(bind=bind)
