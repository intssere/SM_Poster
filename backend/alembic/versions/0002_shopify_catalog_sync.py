"""shopify catalog sync and normalized intelligence

Revision ID: 0002
Revises: 0001
"""

from alembic import op
import sqlalchemy as sa


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("products", sa.Column("collections", sa.JSON(), nullable=True))
    op.add_column("products", sa.Column("shopify_data", sa.JSON(), nullable=True))
    op.add_column("products", sa.Column("shopify_created_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("products", sa.Column("shopify_updated_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE products SET collections = '[]'::json WHERE collections IS NULL")
    op.execute("UPDATE products SET shopify_data = '{}'::json WHERE shopify_data IS NULL")
    op.alter_column("products", "collections", nullable=False, server_default="[]")
    op.alter_column("products", "shopify_data", nullable=False, server_default="{}")

    op.create_table(
        "product_intelligence",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("product_id", sa.String(length=36), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("brand", sa.String(length=255)),
        sa.Column("audience", sa.String(length=100)),
        sa.Column("designer", sa.String(length=255)),
        sa.Column("niche", sa.String(length=255)),
        sa.Column("arabian_classification", sa.String(length=100)),
        sa.Column("fragrance_family", sa.String(length=255)),
        sa.Column("fragrance_notes", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("concentration", sa.String(length=100)),
        sa.Column("size", sa.String(length=100)),
        sa.Column("price_band", sa.String(length=50)),
        sa.Column("gift_suitability", sa.String(length=50)),
        sa.Column("season", sa.String(length=100)),
        sa.Column("occasion", sa.String(length=100)),
        sa.Column("image_quality", sa.Numeric(4, 3), nullable=False, server_default="0"),
        sa.Column("image_available", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("inventory_eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("eligibility_score", sa.Numeric(6, 2), nullable=False, server_default="0"),
        sa.Column("eligibility_status", sa.String(length=30), nullable=False, server_default="INELIGIBLE"),
        sa.Column("eligibility_reasons", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("normalization_status", sa.String(length=30), nullable=False, server_default="UNKNOWN"),
        sa.Column("normalized_data", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_product_intelligence_product_id", "product_intelligence", ["product_id"], unique=True)

    op.create_table(
        "catalog_sync_jobs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("store_id", sa.String(length=36), sa.ForeignKey("stores.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="QUEUED"),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("total_seen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("products_imported", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("products_updated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("products_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text()),
        sa.Column("provider_operation_id", sa.String(length=255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_catalog_sync_jobs_store_id", "catalog_sync_jobs", ["store_id"])
    op.create_index("ix_catalog_sync_jobs_status", "catalog_sync_jobs", ["status"])
    op.create_index(
        "uq_catalog_sync_active",
        "catalog_sync_jobs",
        ["store_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('QUEUED', 'RUNNING')"),
    )


def downgrade():
    op.drop_index("uq_catalog_sync_active", table_name="catalog_sync_jobs")
    op.drop_index("ix_catalog_sync_jobs_status", table_name="catalog_sync_jobs")
    op.drop_index("ix_catalog_sync_jobs_store_id", table_name="catalog_sync_jobs")
    op.drop_table("catalog_sync_jobs")
    op.drop_index("ix_product_intelligence_product_id", table_name="product_intelligence")
    op.drop_table("product_intelligence")
    op.drop_column("products", "shopify_updated_at")
    op.drop_column("products", "shopify_created_at")
    op.drop_column("products", "shopify_data")
    op.drop_column("products", "collections")