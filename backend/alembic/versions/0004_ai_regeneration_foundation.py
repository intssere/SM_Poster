"""AI regeneration settings and immutable content revisions

Revision ID: 0004
Revises: 0003
"""

from alembic import op
import sqlalchemy as sa


revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ai_settings",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("provider_mode", sa.String(length=30), nullable=False, server_default="disabled"),
        sa.Column("decorative_backgrounds_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "content_revisions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("draft_id", sa.String(length=36), sa.ForeignKey("pin_drafts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("parent_revision_id", sa.String(length=36), sa.ForeignKey("content_revisions.id", ondelete="SET NULL")),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("revision_kind", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="REVIEW"),
        sa.Column("headline", sa.String(length=500), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("alt_text", sa.Text(), nullable=False),
        sa.Column("cta", sa.String(length=255), nullable=False),
        sa.Column("content_angle", sa.String(length=255), nullable=False),
        sa.Column("content_angle_key", sa.String(length=120), nullable=False),
        sa.Column("creative_template", sa.String(length=255), nullable=False),
        sa.Column("creative_template_key", sa.String(length=120), nullable=False),
        sa.Column("destination_url", sa.Text(), nullable=False),
        sa.Column("utm_url", sa.Text(), nullable=False),
        sa.Column("keywords", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("facts_used", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("warnings", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("missing_facts", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("unsupported_claims", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("provenance", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("text_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("creative_fingerprint", sa.String(length=64)),
        sa.Column("creative_id", sa.String(length=36), sa.ForeignKey("pin_creatives.id", ondelete="SET NULL")),
        sa.Column("source_image_id", sa.String(length=36), sa.ForeignKey("product_images.id"), nullable=False),
        sa.Column("provider_mode", sa.String(length=30), nullable=False),
        sa.Column("generation_mode", sa.String(length=40), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("draft_id", "version", name="uq_content_revision_version"),
    )
    op.create_index("ix_content_revisions_draft_id", "content_revisions", ["draft_id"])
    op.create_index("ix_content_revisions_parent_revision_id", "content_revisions", ["parent_revision_id"])
    op.create_index("ix_content_revisions_text_fingerprint", "content_revisions", ["text_fingerprint"])
    op.create_index("ix_content_revisions_creative_fingerprint", "content_revisions", ["creative_fingerprint"])
    op.create_index("ix_content_revisions_creative_id", "content_revisions", ["creative_id"])
    op.create_index("ix_content_revisions_source_image_id", "content_revisions", ["source_image_id"])
    op.create_table(
        "content_version_selections",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("draft_id", sa.String(length=36), sa.ForeignKey("pin_drafts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("revision_id", sa.String(length=36), sa.ForeignKey("content_revisions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("selected_by", sa.String(length=255), nullable=False),
        sa.Column("selected_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("draft_id"),
    )
    op.create_index("ix_content_version_selections_draft_id", "content_version_selections", ["draft_id"])
    op.create_index("ix_content_version_selections_revision_id", "content_version_selections", ["revision_id"])


def downgrade():
    op.drop_index("ix_content_version_selections_revision_id", table_name="content_version_selections")
    op.drop_index("ix_content_version_selections_draft_id", table_name="content_version_selections")
    op.drop_table("content_version_selections")
    for index in (
        "ix_content_revisions_source_image_id",
        "ix_content_revisions_creative_id",
        "ix_content_revisions_creative_fingerprint",
        "ix_content_revisions_text_fingerprint",
        "ix_content_revisions_parent_revision_id",
        "ix_content_revisions_draft_id",
    ):
        op.drop_index(index, table_name="content_revisions")
    op.drop_table("content_revisions")
    op.drop_table("ai_settings")