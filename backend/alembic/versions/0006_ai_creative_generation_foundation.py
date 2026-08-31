"""AI image-background assets and reviewable video specifications

Revision ID: 0006
Revises: 0005
"""

from alembic import op
import sqlalchemy as sa


revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("ai_settings", sa.Column("image_model", sa.String(length=120), nullable=False, server_default="gpt-image-1"))
    op.add_column("ai_settings", sa.Column("video_model", sa.String(length=120), nullable=False, server_default="gpt-4o-mini"))
    op.add_column("ai_settings", sa.Column("per_request_cost_usd", sa.Float(), nullable=False, server_default="0.25"))
    op.add_column("content_revisions", sa.Column("generation_type", sa.String(length=40), nullable=False, server_default="copy"))
    op.add_column("content_revisions", sa.Column("intended_channel", sa.String(length=40), nullable=False, server_default="pinterest"))
    op.add_column("content_revisions", sa.Column("content_payload", sa.JSON()))
    op.add_column("content_revisions", sa.Column("video_spec", sa.JSON()))
    op.add_column("content_revisions", sa.Column("background_asset_id", sa.String(length=36)))
    op.create_index("ix_content_revisions_background_asset_id", "content_revisions", ["background_asset_id"])
    op.add_column("ai_request_telemetry", sa.Column("request_type", sa.String(length=40), nullable=False, server_default="generation"))
    op.add_column("ai_request_telemetry", sa.Column("generation_type", sa.String(length=40), nullable=False, server_default="copy"))
    op.add_column("ai_request_telemetry", sa.Column("fallback_reason", sa.String(length=120)))
    op.add_column("ai_request_telemetry", sa.Column("validation_failure_reason", sa.String(length=120)))
    op.add_column("ai_request_telemetry", sa.Column("draft_id", sa.String(length=36), sa.ForeignKey("pin_drafts.id", ondelete="SET NULL")))
    op.create_index("ix_ai_request_telemetry_draft_id", "ai_request_telemetry", ["draft_id"])
    op.create_table(
        "ai_generated_assets",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("draft_id", sa.String(length=36), sa.ForeignKey("pin_drafts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("asset_type", sa.String(length=40), nullable=False),
        sa.Column("provider", sa.String(length=30), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="REVIEW"),
        sa.Column("storage_path", sa.Text()),
        sa.Column("mime_type", sa.String(length=80)),
        sa.Column("width", sa.Integer()),
        sa.Column("height", sa.Integer()),
        sa.Column("size_bytes", sa.Integer()),
        sa.Column("sha256", sa.String(length=64)),
        sa.Column("prompt_hash", sa.String(length=64), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_ai_generated_assets_draft_id", "ai_generated_assets", ["draft_id"])
    op.create_index("ix_ai_generated_assets_sha256", "ai_generated_assets", ["sha256"])
    op.create_index("ix_ai_generated_assets_created_at", "ai_generated_assets", ["created_at"])


def downgrade():
    op.drop_index("ix_ai_generated_assets_created_at", table_name="ai_generated_assets")
    op.drop_index("ix_ai_generated_assets_sha256", table_name="ai_generated_assets")
    op.drop_index("ix_ai_generated_assets_draft_id", table_name="ai_generated_assets")
    op.drop_table("ai_generated_assets")
    op.drop_index("ix_ai_request_telemetry_draft_id", table_name="ai_request_telemetry")
    op.drop_column("ai_request_telemetry", "draft_id")
    op.drop_column("ai_request_telemetry", "validation_failure_reason")
    op.drop_column("ai_request_telemetry", "fallback_reason")
    op.drop_column("ai_request_telemetry", "generation_type")
    op.drop_column("ai_request_telemetry", "request_type")
    op.drop_index("ix_content_revisions_background_asset_id", table_name="content_revisions")
    for column in ("background_asset_id", "video_spec", "content_payload", "intended_channel", "generation_type"):
        op.drop_column("content_revisions", column)
    op.drop_column("ai_settings", "per_request_cost_usd")
    op.drop_column("ai_settings", "video_model")
    op.drop_column("ai_settings", "image_model")