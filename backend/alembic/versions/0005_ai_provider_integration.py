"""Provider configuration and non-secret AI usage telemetry

Revision ID: 0005
Revises: 0004
"""

from alembic import op
import sqlalchemy as sa


revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("ai_settings", sa.Column("local_base_url", sa.String(length=500), nullable=False, server_default="http://127.0.0.1:11434"))
    op.add_column("ai_settings", sa.Column("local_model", sa.String(length=120), nullable=False, server_default="llama3.2:3b"))
    op.add_column("ai_settings", sa.Column("hosted_model", sa.String(length=120), nullable=False, server_default="gpt-4o-mini"))
    op.add_column("ai_settings", sa.Column("request_timeout_seconds", sa.Integer(), nullable=False, server_default="30"))
    op.add_column("ai_settings", sa.Column("daily_budget_usd", sa.Float(), nullable=False, server_default="1.0"))
    op.add_column("ai_settings", sa.Column("monthly_budget_usd", sa.Float(), nullable=False, server_default="10.0"))
    op.add_column("ai_settings", sa.Column("pricing_metadata", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("content_revisions", sa.Column("ai_telemetry_id", sa.String(length=36)))
    op.add_column("content_revisions", sa.Column("estimated_cost_usd", sa.Float()))
    op.add_column("content_revisions", sa.Column("actual_cost_usd", sa.Float()))
    op.create_index("ix_content_revisions_ai_telemetry_id", "content_revisions", ["ai_telemetry_id"])
    op.create_table(
        "ai_request_telemetry",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("provider", sa.String(length=30), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("operation", sa.String(length=40), nullable=False),
        sa.Column("prompt_tokens", sa.Integer()),
        sa.Column("completion_tokens", sa.Integer()),
        sa.Column("total_tokens", sa.Integer()),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("failure_code", sa.String(length=80)),
        sa.Column("estimated_cost_usd", sa.Float()),
        sa.Column("actual_cost_usd", sa.Float()),
        sa.Column("fallback_used", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_ai_request_telemetry_provider", "ai_request_telemetry", ["provider"])
    op.create_index("ix_ai_request_telemetry_created_at", "ai_request_telemetry", ["created_at"])


def downgrade():
    op.drop_index("ix_ai_request_telemetry_created_at", table_name="ai_request_telemetry")
    op.drop_index("ix_ai_request_telemetry_provider", table_name="ai_request_telemetry")
    op.drop_table("ai_request_telemetry")
    op.drop_index("ix_content_revisions_ai_telemetry_id", table_name="content_revisions")
    op.drop_column("content_revisions", "actual_cost_usd")
    op.drop_column("content_revisions", "estimated_cost_usd")
    op.drop_column("content_revisions", "ai_telemetry_id")
    for column in (
        "pricing_metadata",
        "monthly_budget_usd",
        "daily_budget_usd",
        "request_timeout_seconds",
        "hosted_model",
        "local_model",
        "local_base_url",
    ):
        op.drop_column("ai_settings", column)