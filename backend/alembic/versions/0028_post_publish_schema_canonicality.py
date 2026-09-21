"""Repair exact empty post-Replit schema drift and advance canonical head.

Revision ID: 0028
Revises: 0027
"""
from alembic import op
import sqlalchemy as sa

from app.db.migration_adoption import (
    POST_PUBLISH_DRIFT_DROP_ORDER,
    reconcile_post_publish_drift,
    verify_post_publish_repair,
)


revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def _create_analytics_tables() -> None:
    op.create_table(
        "pinterest_analytics_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "publication_id",
            sa.String(36),
            sa.ForeignKey("pin_publications.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("pinterest_pin_id", sa.String(80), nullable=False),
        sa.Column("metric_policy_version", sa.String(80), nullable=False),
        sa.Column("observation_window", sa.String(4), nullable=False),
        sa.Column("range_start", sa.Date(), nullable=False),
        sa.Column("range_end", sa.Date(), nullable=False),
        sa.Column("provider_payload_fingerprint", sa.String(64), nullable=False),
        sa.Column("impressions", sa.Integer(), nullable=False),
        sa.Column("saves", sa.Integer(), nullable=False),
        sa.Column("pin_clicks", sa.Integer(), nullable=False),
        sa.Column("outbound_clicks", sa.Integer(), nullable=False),
        sa.Column("engagements", sa.Integer(), nullable=False),
        sa.Column("save_rate", sa.Numeric(24, 12), nullable=False),
        sa.Column("pin_click_rate", sa.Numeric(24, 12), nullable=False),
        sa.Column("outbound_click_rate", sa.Numeric(24, 12), nullable=False),
        sa.Column("engagement_rate", sa.Numeric(24, 12), nullable=False),
        sa.Column("safe_metric_map", sa.JSON(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "publication_id",
            "observation_window",
            "metric_policy_version",
            name="uq_pinterest_analytics_observation",
        ),
        sa.CheckConstraint(
            "observation_window IN ('D1','D7','D30','D90')",
            name="ck_pinterest_analytics_window",
        ),
        sa.CheckConstraint(
            "impressions >= 0 AND saves >= 0 AND pin_clicks >= 0 "
            "AND outbound_clicks >= 0 AND engagements >= 0",
            name="ck_pinterest_analytics_counts_nonnegative",
        ),
    )
    op.create_index(
        "ix_pinterest_analytics_snapshots_publication_id",
        "pinterest_analytics_snapshots",
        ["publication_id"],
    )
    op.create_index(
        "ix_pinterest_analytics_snapshots_payload_fp",
        "pinterest_analytics_snapshots",
        ["provider_payload_fingerprint"],
    )

    op.create_table(
        "pinterest_analytics_ingestion_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "publication_id",
            sa.String(36),
            sa.ForeignKey("pin_publications.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("observation_window", sa.String(4), nullable=False),
        sa.Column("pinterest_pin_id", sa.String(80), nullable=False),
        sa.Column("range_start", sa.Date(), nullable=False),
        sa.Column("range_end", sa.Date(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="STARTED"),
        sa.Column("provider_payload_fingerprint", sa.String(64)),
        sa.Column(
            "snapshot_id",
            sa.String(36),
            sa.ForeignKey("pinterest_analytics_snapshots.id", ondelete="SET NULL"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(100)),
        sa.Column("safe_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "observation_window IN ('D1','D7','D30','D90')",
            name="ck_pinterest_analytics_run_window",
        ),
        sa.CheckConstraint(
            "status IN ('STARTED','SUCCEEDED','FAILED')",
            name="ck_pinterest_analytics_run_status",
        ),
    )
    op.create_index(
        "ix_pinterest_analytics_runs_publication_id",
        "pinterest_analytics_ingestion_runs",
        ["publication_id"],
    )
    op.create_index(
        "ix_pinterest_analytics_runs_status",
        "pinterest_analytics_ingestion_runs",
        ["status"],
    )
    op.create_index(
        "ix_pinterest_analytics_runs_payload_fp",
        "pinterest_analytics_ingestion_runs",
        ["provider_payload_fingerprint"],
    )
    op.create_index(
        "ix_pinterest_analytics_runs_snapshot_id",
        "pinterest_analytics_ingestion_runs",
        ["snapshot_id"],
    )


def _create_learning_table() -> None:
    op.create_table(
        "pinterest_learning_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "store_id",
            sa.String(36),
            sa.ForeignKey("stores.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("policy_version", sa.String(80), nullable=False),
        sa.Column("as_of_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("learning_fingerprint", sa.String(64), nullable=False),
        sa.Column("publication_count", sa.Integer(), nullable=False),
        sa.Column("snapshot_count", sa.Integer(), nullable=False),
        sa.Column("global_priors", sa.JSON(), nullable=False),
        sa.Column("rankings", sa.JSON(), nullable=False),
        sa.Column("optimizer_ready", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "store_id",
            "policy_version",
            "input_fingerprint",
            name="uq_pinterest_learning_input",
        ),
        sa.UniqueConstraint(
            "learning_fingerprint",
            name="uq_pinterest_learning_fingerprint",
        ),
        sa.CheckConstraint(
            "publication_count >= 0 AND snapshot_count >= 0",
            name="ck_pinterest_learning_counts_nonnegative",
        ),
    )
    op.create_index(
        "ix_pinterest_learning_snapshots_store_id",
        "pinterest_learning_snapshots",
        ["store_id"],
    )
    op.create_index(
        "ix_pinterest_learning_snapshots_as_of_at",
        "pinterest_learning_snapshots",
        ["as_of_at"],
    )
    op.create_index(
        "ix_pinterest_learning_snapshots_input_fp",
        "pinterest_learning_snapshots",
        ["input_fingerprint"],
    )


def _create_optimizer_table() -> None:
    op.create_table(
        "pinterest_optimizer_applications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "plan_id",
            sa.String(36),
            sa.ForeignKey("pinterest_portfolio_plans.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("plan_fingerprint_snapshot", sa.String(64), nullable=False),
        sa.Column("optimizer_policy_version", sa.String(80), nullable=False),
        sa.Column("optimizer_fingerprint", sa.String(64), nullable=False),
        sa.Column("learning_fingerprint", sa.String(64)),
        sa.Column("input_state_fingerprint", sa.String(64), nullable=False),
        sa.Column("frozen_item_count", sa.Integer(), nullable=False),
        sa.Column("optimizable_item_count", sa.Integer(), nullable=False),
        sa.Column("exploit_count", sa.Integer(), nullable=False),
        sa.Column("explore_count", sa.Integer(), nullable=False),
        sa.Column("recommendation_snapshot", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="APPLIED"),
        sa.Column("applied_by", sa.String(255), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "plan_id",
            name="uq_pinterest_optimizer_application_plan",
        ),
        sa.UniqueConstraint(
            "optimizer_fingerprint",
            name="uq_pinterest_optimizer_application_fingerprint",
        ),
        sa.CheckConstraint(
            "status = 'APPLIED'",
            name="ck_pinterest_optimizer_application_status",
        ),
        sa.CheckConstraint(
            "frozen_item_count >= 0 AND optimizable_item_count >= 0 "
            "AND exploit_count >= 0 AND explore_count >= 0",
            name="ck_pinterest_optimizer_application_counts",
        ),
    )
    op.create_index(
        "ix_pinterest_optimizer_applications_plan_id",
        "pinterest_optimizer_applications",
        ["plan_id"],
    )
    op.create_index(
        "ix_pinterest_optimizer_applications_input_state",
        "pinterest_optimizer_applications",
        ["input_state_fingerprint"],
    )
    op.create_index(
        "ix_pinterest_optimizer_applications_status",
        "pinterest_optimizer_applications",
        ["status"],
    )


def _create_execution_table() -> None:
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


def _create_destination_table() -> None:
    op.create_table(
        "pinterest_autonomous_destination_runs",
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
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="STARTED"),
        sa.Column("stage", sa.String(40), nullable=False, server_default="STARTED"),
        sa.Column(
            "board_provisioning_attempt_id",
            sa.String(36),
            sa.ForeignKey("pinterest_board_provisioning_attempts.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "pinterest_board_record_id",
            sa.String(36),
            sa.ForeignKey("pinterest_boards.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "autonomous_execution_run_id",
            sa.String(36),
            sa.ForeignKey("pinterest_autonomous_execution_runs.id", ondelete="RESTRICT"),
        ),
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
            name="uq_pinterest_auto_destination_item",
        ),
        sa.UniqueConstraint(
            "input_fingerprint",
            name="uq_pinterest_auto_destination_fingerprint",
        ),
        sa.CheckConstraint(
            "status IN ('STARTED','SUCCEEDED','FAILED','UNKNOWN')",
            name="ck_pinterest_auto_destination_status",
        ),
        sa.CheckConstraint(
            "stage IN ('STARTED','BOARD_PROVISIONING','BOARD_CREATED','BOARD_SYNC_PENDING','BOARD_READY','EXECUTION_READY')",
            name="ck_pinterest_auto_destination_stage",
        ),
    )
    op.create_index(
        "ix_pinterest_auto_destination_plan_id",
        "pinterest_autonomous_destination_runs",
        ["plan_id"],
    )
    op.create_index(
        "ix_pinterest_auto_destination_status",
        "pinterest_autonomous_destination_runs",
        ["status"],
    )
    op.create_index(
        "ix_pinterest_auto_destination_stage",
        "pinterest_autonomous_destination_runs",
        ["stage"],
    )
    op.create_index(
        "ix_pinterest_auto_destination_plan_stage",
        "pinterest_autonomous_destination_runs",
        ["plan_id", "stage"],
    )
    op.create_index(
        "ix_pinterest_auto_destination_status_stage",
        "pinterest_autonomous_destination_runs",
        ["status", "stage"],
    )
    op.create_index(
        "ix_pinterest_auto_destination_provisioning_attempt",
        "pinterest_autonomous_destination_runs",
        ["board_provisioning_attempt_id"],
    )
    op.create_index(
        "ix_pinterest_auto_destination_board_record",
        "pinterest_autonomous_destination_runs",
        ["pinterest_board_record_id"],
    )
    op.create_index(
        "ix_pinterest_auto_destination_execution_run",
        "pinterest_autonomous_destination_runs",
        ["autonomous_execution_run_id"],
    )


def upgrade():
    bind = op.get_bind()
    if not reconcile_post_publish_drift(bind, revision):
        return

    for table in POST_PUBLISH_DRIFT_DROP_ORDER:
        op.drop_table(table)

    _create_analytics_tables()
    _create_learning_table()
    _create_optimizer_table()
    _create_execution_table()
    _create_destination_table()
    verify_post_publish_repair(bind)


def downgrade():
    # Revision 0028 has no net schema contract beyond canonicalizing the
    # existing 0023-0027 tables, so downgrade changes bookkeeping only.
    pass
