"""Durable autonomous portfolio item execution state machine.

Revision ID: 0026
Revises: 0025
"""
from alembic import op
import sqlalchemy as sa
from app.db.migration_adoption import adopt_preapplied_revision

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade():
    if adopt_preapplied_revision(op.get_bind(), revision):
        return
    op.create_table(
        "pinterest_autonomous_execution_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "portfolio_item_id",
            sa.String(36),
            sa.ForeignKey("pinterest_portfolio_plan_items.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "plan_id",
            sa.String(36),
            sa.ForeignKey("pinterest_portfolio_plans.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "optimizer_application_id",
            sa.String(36),
            sa.ForeignKey("pinterest_optimizer_applications.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="STARTED"),
        sa.Column("stage", sa.String(40), nullable=False, server_default="STARTED"),
        sa.Column(
            "seo_brief_id",
            sa.String(36),
            sa.ForeignKey("pinterest_seo_briefs.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "generation_run_id",
            sa.String(36),
            sa.ForeignKey("pinterest_autonomous_generation_runs.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "approval_id",
            sa.String(36),
            sa.ForeignKey("pin_approvals.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "publication_id",
            sa.String(36),
            sa.ForeignKey("pin_publications.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "routine_permit_id",
            sa.String(36),
            sa.ForeignKey("routine_dispatch_permits.id", ondelete="SET NULL"),
        ),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("safe_metadata", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "portfolio_item_id",
            name="uq_pinterest_auto_exec_portfolio_item",
        ),
        sa.UniqueConstraint(
            "input_fingerprint",
            name="uq_pinterest_auto_exec_input_fingerprint",
        ),
        sa.CheckConstraint(
            "status IN ('STARTED','SUCCEEDED','FAILED')",
            name="ck_pinterest_auto_exec_status",
        ),
        sa.CheckConstraint(
            "stage IN ('STARTED','SEO_READY','GENERATED','AUTHORIZED','PUBLICATION_CREATED','PERMITTED')",
            name="ck_pinterest_auto_exec_stage",
        ),
    )
    op.create_index(
        "ix_pinterest_auto_exec_plan_id",
        "pinterest_autonomous_execution_runs",
        ["plan_id"],
    )
    op.create_index(
        "ix_pinterest_auto_exec_optimizer_app",
        "pinterest_autonomous_execution_runs",
        ["optimizer_application_id"],
    )
    op.create_index(
        "ix_pinterest_auto_exec_status",
        "pinterest_autonomous_execution_runs",
        ["status"],
    )
    op.create_index(
        "ix_pinterest_auto_exec_stage",
        "pinterest_autonomous_execution_runs",
        ["stage"],
    )
    op.create_index(
        "ix_pinterest_auto_exec_scheduled_for",
        "pinterest_autonomous_execution_runs",
        ["scheduled_for"],
    )
    op.create_index(
        "ix_pinterest_auto_exec_plan_stage",
        "pinterest_autonomous_execution_runs",
        ["plan_id", "stage"],
    )
    op.create_index(
        "ix_pinterest_auto_exec_seo_brief",
        "pinterest_autonomous_execution_runs",
        ["seo_brief_id"],
    )
    op.create_index(
        "ix_pinterest_auto_exec_generation",
        "pinterest_autonomous_execution_runs",
        ["generation_run_id"],
    )
    op.create_index(
        "ix_pinterest_auto_exec_approval",
        "pinterest_autonomous_execution_runs",
        ["approval_id"],
    )
    op.create_index(
        "ix_pinterest_auto_exec_publication",
        "pinterest_autonomous_execution_runs",
        ["publication_id"],
    )
    op.create_index(
        "ix_pinterest_auto_exec_permit",
        "pinterest_autonomous_execution_runs",
        ["routine_permit_id"],
    )


def downgrade():
    op.drop_table("pinterest_autonomous_execution_runs")
