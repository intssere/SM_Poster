"""Pinterest organic performance analytics foundation.

Revision ID: 0023
Revises: 0022
"""
from alembic import op
import sqlalchemy as sa

from app.db.preapplied_schema_adoption import adopt_preapplied_revision

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade():
    if adopt_preapplied_revision(op.get_bind(), "0023"):
        return
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


def downgrade():
    op.drop_table("pinterest_analytics_ingestion_runs")
    op.drop_table("pinterest_analytics_snapshots")
