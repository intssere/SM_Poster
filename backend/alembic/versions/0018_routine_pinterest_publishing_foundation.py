"""Controlled routine Buffer-mediated Pinterest publishing foundation."""
from alembic import op
import sqlalchemy as sa

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "routine_dispatch_permits",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("publication_id", sa.String(36), sa.ForeignKey("pin_publications.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("dispatch_provider", sa.String(40), nullable=False, server_default="buffer"),
        sa.Column("approval_id", sa.String(36), sa.ForeignKey("pin_approvals.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("pinterest_board_record_id", sa.String(36), sa.ForeignKey("pinterest_boards.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("publication_fingerprint", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("scheduled_for_snapshot", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quality_policy_version", sa.String(80), nullable=False),
        sa.Column("quality_snapshot", sa.JSON(), nullable=False),
        sa.Column("duplicate_snapshot", sa.JSON(), nullable=False),
        sa.Column("readiness_snapshot", sa.JSON(), nullable=False),
        sa.Column("authorized_by", sa.String(255), nullable=False),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="ACTIVE"),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_by", sa.String(255)),
        sa.Column("revoke_reason", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("dispatch_provider = 'buffer'", name="ck_routine_dispatch_permit_provider"),
        sa.CheckConstraint("status IN ('ACTIVE','CONSUMED','REVOKED','EXPIRED')", name="ck_routine_dispatch_permit_status"),
    )
    op.create_index("ix_routine_dispatch_permits_publication_id", "routine_dispatch_permits", ["publication_id"])
    op.create_index("ix_routine_dispatch_permit_expires_at", "routine_dispatch_permits", ["expires_at"])
    op.create_index(
        "uq_routine_dispatch_permit_active", "routine_dispatch_permits", ["publication_id"], unique=True,
        sqlite_where=sa.text("status = 'ACTIVE'"), postgresql_where=sa.text("status = 'ACTIVE'"),
    )

    op.create_table(
        "routine_publishing_control",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("state", sa.String(20), nullable=False, server_default="PAUSED"),
        sa.Column("pause_reason", sa.String(255)),
        sa.Column("paused_at", sa.DateTime(timezone=True)),
        sa.Column("paused_by", sa.String(255)),
        sa.Column("last_unknown_publication_id", sa.String(36)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("state IN ('PAUSED','DRY_RUN','LIVE')", name="ck_routine_publishing_control_state"),
    )

    op.create_table(
        "routine_publishing_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(20), nullable=False, server_default="RUNNING"),
        sa.Column("scanned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("eligible", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claimed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dispatched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("published", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unknown", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(100)),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.CheckConstraint("status IN ('RUNNING','SUCCEEDED','FAILED','BLOCKED')", name="ck_routine_publishing_run_status"),
    )
    op.create_index("ix_routine_publishing_run_started_at", "routine_publishing_runs", ["started_at"])
    op.create_index(
        "uq_routine_publishing_run_running", "routine_publishing_runs", ["status"], unique=True,
        sqlite_where=sa.text("status = 'RUNNING'"), postgresql_where=sa.text("status = 'RUNNING'"),
    )

    op.create_table(
        "routine_attempt_boundaries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("attempt_id", sa.String(36), sa.ForeignKey("publication_attempts.id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("publication_id", sa.String(36), sa.ForeignKey("pin_publications.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("routine_dispatch_permit_id", sa.String(36), sa.ForeignKey("routine_dispatch_permits.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_mutation_started_at", sa.DateTime(timezone=True)),
        sa.Column("safe_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_routine_attempt_boundaries_attempt_id", "routine_attempt_boundaries", ["attempt_id"])
    op.create_index("ix_routine_attempt_boundaries_publication_id", "routine_attempt_boundaries", ["publication_id"])
    op.create_index("ix_routine_attempt_boundaries_routine_dispatch_permit_id", "routine_attempt_boundaries", ["routine_dispatch_permit_id"])
    op.create_index("ix_routine_attempt_boundaries_provider_mutation_started_at", "routine_attempt_boundaries", ["provider_mutation_started_at"])


def downgrade():
    op.drop_table("routine_attempt_boundaries")
    op.drop_table("routine_publishing_runs")
    op.drop_table("routine_publishing_control")
    op.drop_table("routine_dispatch_permits")
